//! `xpst stream` — destinations, app credentials, ingest keys and the MediaMTX
//! relay config.
//!
//! Parity targets: `src/xpst/cli.py::stream_*`, `src/xpst/stream/destinations.py`,
//! `src/xpst/stream/ingest.py` (the credential half) and
//! `src/xpst/stream/relay.py`.
//!
//! The registry, the credential resolution, the relay destinations and the two
//! payloads live in `xpst_surface::stream`, because two surfaces serve them: these
//! commands and the `xpst_stream` MCP tool. This module is the clap surface plus
//! the plain-text renderers.
//!
//! Nothing here uploads video or contacts a platform: `destinations` reports
//! which platforms can hand an app an ingest key and which are copy/paste by
//! design, `set-key`/`clear-key` write the encrypted store, and `relay-config`
//! renders the MediaMTX file.
//!
//! **Divergences, recorded in the ledger note:** text mode renders plain lines
//! instead of Rich tables (`--json` is the parity surface), and the interactive
//! prompts (`set-key` without `--ingest-url`/`--key`, `app-credentials` without
//! its flags) read one line from stdin instead of Click's hidden-input prompt.

use std::io::Write;
use std::path::PathBuf;

use clap::ArgMatches;
use xpst_auth::credentials::CredentialStore;
use xpst_config::Value;

use crate::commands::config_view::{self, ConfigView};
use crate::output::{
    boolean, null, object, python_repr, string, wrap_at, write_python_json, write_text,
};
use crate::{exit, flag, leaf_flag, leaf_string, GlobalOptions};

// The data layer is shared with the MCP surface, so it lives below both crates.
pub use xpst_surface::stream::{
    build_relay_config, clear_app_credentials, load_app_credentials, load_destinations, mask,
    registry, relay_summary, render_relay_config, store_app_credentials, AppCredentials,
    AppRegistration, FetchReadiness, IngestPath, Registry, RelayDestination, APP_CREDENTIAL_PREFIX,
    DEFAULT_PATH_NAME, DEFAULT_RTMP_PORT, KEY_PREFIX,
};

/// Rich's default console width when stdout is not a TTY.
const WIDTH: usize = 80;

fn json_mode(global: &GlobalOptions, leaf: Option<&ArgMatches>) -> bool {
    global.json || leaf.is_some_and(|m| flag(m, "opt:json"))
}

fn resolve(
    global: &GlobalOptions,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> Result<(ConfigView, PathBuf), i32> {
    match config_view::resolve(global) {
        Ok(view) => {
            let dir = view.config_dir().to_path_buf();
            Ok((view, dir))
        }
        Err(error) => Err(crate::commands::bootstrap::report(out, err, &error)),
    }
}

/// `xpst stream destinations`.
pub fn destinations(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = json_mode(global, leaf);
    let (view, dir) = match resolve(global, out, err) {
        Ok(resolved) => resolved,
        Err(code) => return code,
    };

    let payload = xpst_surface::stream::destination_status(&dir, &view);
    let ok = payload.get("ok").and_then(Value::as_bool).unwrap_or(false);

    if json {
        let _ = write_python_json(out, &payload);
        if !ok {
            return exit::CONFIG_ERROR;
        }
        return exit::SUCCESS;
    }

    let rows = payload
        .get("platforms")
        .and_then(Value::as_sequence)
        .map(<[Value]>::to_vec)
        .unwrap_or_default();
    let mut text = String::from("Live stream destinations\n");
    for row in &rows {
        let ingest_class = row
            .get("ingest_class")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if ingest_class == "unsupported" {
            continue;
        }
        let display_name = row
            .get("display_name")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let key_present = row
            .get("key_present")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let key = if key_present { "stored" } else { "—" };
        let fetch = match row.get("fetch") {
            Some(Value::Null) | None => "—".to_string(),
            Some(fetch) => {
                if fetch
                    .get("credentials_present")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
                {
                    "ready".to_string()
                } else {
                    fetch
                        .get("missing")
                        .and_then(Value::as_sequence)
                        .map(|items| {
                            items
                                .iter()
                                .filter_map(Value::as_str)
                                .collect::<Vec<_>>()
                                .join(", ")
                        })
                        .unwrap_or_default()
                }
            }
        };
        text.push_str(&format!(
            "  {display_name} | {ingest_class} | key {key} | fetch {fetch}\n"
        ));
    }
    if let Some(error) = payload.get("error").and_then(Value::as_str) {
        text.push_str(&format!("{error}\n"));
    }
    let _ = write_text(out, &text);
    if !ok {
        return exit::CONFIG_ERROR;
    }
    exit::SUCCESS
}

/// `xpst stream app-credentials PLATFORM`.
pub fn app_credentials(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = json_mode(global, leaf);
    let registry = match registry() {
        Ok(registry) => registry,
        Err(error) => {
            let _ = writeln!(err, "xpst stream app-credentials: {error}");
            return exit::GENERAL;
        }
    };
    let platform = leaf_string(leaf, "arg:platform").unwrap_or_default();
    let name = platform.trim().to_lowercase();
    let Some(spec) = registry.registration(&name) else {
        let mut known: Vec<String> = registry
            .registrations
            .iter()
            .map(|row| row.platform.clone())
            .collect();
        known.sort();
        let message = format!(
            "{} does not use xPST app credentials (known: {})",
            python_repr(&platform),
            known.join(", ")
        );
        if json {
            let payload = object(vec![
                ("ok", boolean(false)),
                ("code", string("not_supported")),
                ("message", string(message.clone())),
            ]);
            let _ = write_python_json(out, &payload);
        } else {
            let _ = write_text(out, &message);
            let _ = write_text(
                out,
                "YouTube reuses `xpst auth youtube`; Facebook reuses `xpst auth facebook`.",
            );
        }
        return exit::CONFIG_ERROR;
    };

    let (view, dir) = match resolve(global, out, err) {
        Ok(resolved) => resolved,
        Err(code) => return code,
    };
    let _ = view;
    let store = CredentialStore::new(&dir);

    if leaf_flag(leaf, "opt:clear") {
        let removed = clear_app_credentials(&dir, &name);
        if json {
            let payload = object(vec![
                ("ok", boolean(removed)),
                ("platform", string(name.clone())),
                ("removed", boolean(removed)),
            ]);
            let _ = write_python_json(out, &payload);
            return exit::SUCCESS;
        }
        let _ = write_text(
            out,
            &if removed {
                format!("Removed the stored {} app credentials.", spec.display_name)
            } else {
                format!("No stored {} app credentials.", spec.display_name)
            },
        );
        return exit::SUCCESS;
    }

    let client_id = leaf_string(leaf, "opt:client-id");
    let access_token = leaf_string(leaf, "opt:access-token");
    let user_id = leaf_string(leaf, "opt:user-id").unwrap_or_default();

    if client_id.is_none() && access_token.is_none() {
        let current = load_app_credentials(&dir, &name, &registry, &store);
        if json {
            let payload = object(vec![
                ("ok", boolean(true)),
                ("credentials", current.masked()),
                ("registration", spec.to_json()),
            ]);
            let _ = write_python_json(out, &payload);
            return exit::SUCCESS;
        }
        let mut text = format!(
            "{} app credentials: {}\n",
            spec.display_name,
            if current.complete() && !current.user_id.is_empty() {
                "stored"
            } else {
                "incomplete"
            }
        );
        text.push_str(&format!("  client id:     {}\n", mask(&current.client_id)));
        text.push_str(&format!(
            "  access token:  {}\n",
            mask(&current.access_token)
        ));
        text.push_str(&format!(
            "  user id:       {}\n",
            if current.user_id.is_empty() {
                "—".to_string()
            } else {
                current.user_id.clone()
            }
        ));
        if !current.source.is_empty() {
            text.push_str(&format!("  source:        {}\n", current.source));
        }
        text.push_str("Registration steps\n");
        for step in &spec.steps {
            text.push_str(&format!("  • {step}\n"));
        }
        text.push_str(&format!("  scopes: {}\n", spec.scopes.join(", ")));
        let _ = write_text(out, &text);
        return exit::SUCCESS;
    }

    let resolved_id =
        client_id.unwrap_or_else(|| prompt(&format!("{} client id", spec.display_name)));
    let resolved_token = access_token.unwrap_or_else(|| prompt("Access token"));
    let resolved_user = if user_id.is_empty() {
        prompt(&format!("{} broadcaster/channel id", spec.display_name))
    } else {
        user_id
    };

    match store_app_credentials(
        &dir,
        &name,
        &registry,
        &resolved_id,
        &resolved_token,
        &resolved_user,
    ) {
        Ok(stored) => {
            if json {
                let mut ordered = xpst_config::Mapping::new();
                ordered.insert(Value::from("ok"), boolean(true));
                if let Value::Mapping(map) = stored.masked() {
                    for (key, value) in map {
                        ordered.insert(key, value);
                    }
                }
                let _ = write_python_json(out, &Value::Mapping(ordered));
                return exit::SUCCESS;
            }
            let _ = write_text(
                out,
                &format!(
                    "Stored {} app credentials (encrypted at rest)",
                    spec.display_name
                ),
            );
            let _ = write_text(out, &format!("Try `xpst stream fetch-key {name}`."));
            exit::SUCCESS
        }
        Err((code, message)) => {
            if json {
                let payload = object(vec![
                    ("ok", boolean(false)),
                    ("code", string(code)),
                    ("platform", string(name.clone())),
                    ("message", string(message.clone())),
                    ("action", string("")),
                    ("docs_url", string("")),
                    ("status", null()),
                ]);
                let _ = write_python_json(out, &payload);
            } else {
                let _ = write_text(out, &message);
            }
            exit::CONFIG_ERROR
        }
    }
}

/// One line from stdin, trimmed (Click's `prompt`).
fn prompt(label: &str) -> String {
    let mut stdout = std::io::stdout();
    let _ = write!(stdout, "{label}: ");
    let _ = stdout.flush();
    let mut line = String::new();
    let _ = std::io::stdin().read_line(&mut line);
    line.trim().to_string()
}

/// `xpst stream set-key PLATFORM`.
pub fn set_key(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = json_mode(global, leaf);
    let registry = match registry() {
        Ok(registry) => registry,
        Err(error) => {
            let _ = writeln!(err, "xpst stream set-key: {error}");
            return exit::GENERAL;
        }
    };
    let platform = leaf_string(leaf, "arg:platform").unwrap_or_default();
    let name = platform.trim().to_lowercase();
    let Some(row) = registry.path(&name) else {
        let mut known: Vec<String> = registry
            .paths
            .iter()
            .map(|row| row.platform.clone())
            .collect();
        known.sort();
        let _ = write_text(
            out,
            &wrap_at(
                &format!(
                    "Unknown stream platform '{platform}'. Known: {}",
                    known.join(", ")
                ),
                WIDTH,
            ),
        );
        return exit::CONFIG_ERROR;
    };
    if row.ingest_class == "unsupported" {
        let _ = write_text(
            out,
            &format!("{} has no live ingest path: {}", row.display_name, row.note),
        );
        return exit::CONFIG_ERROR;
    }

    let (_, dir) = match resolve(global, out, err) {
        Ok(resolved) => resolved,
        Err(code) => return code,
    };

    let ingest_url = leaf_string(leaf, "opt:ingest-url")
        .unwrap_or_else(|| prompt(&format!("{} ingest URL", row.display_name)));
    let stream_key = leaf_string(leaf, "opt:key").unwrap_or_else(|| prompt("Stream key"));

    let destination = match RelayDestination::new(&registry, &name, &ingest_url, &stream_key) {
        Ok(destination) => destination,
        Err(error) => {
            if json {
                let payload = object(vec![
                    ("ok", boolean(false)),
                    ("error", string(error.clone())),
                    ("platform", string(platform.clone())),
                ]);
                let _ = write_python_json(out, &payload);
            }
            let _ = write_text(out, &wrap_at(&error, WIDTH));
            return exit::CONFIG_ERROR;
        }
    };

    let payload = serde_json::json!({
        "ingest_url": destination.ingest_url,
        "stream_key": destination.stream_key,
    });
    if let Err(error) = CredentialStore::new(&dir)
        .store_json(&format!("{KEY_PREFIX}{}", destination.platform), &payload)
    {
        if json {
            let payload = object(vec![
                ("ok", boolean(false)),
                ("error", string(error.clone())),
                ("platform", string(platform.clone())),
            ]);
            let _ = write_python_json(out, &payload);
        }
        let _ = write_text(out, &error);
        return exit::CONFIG_ERROR;
    }

    if json {
        let mut ordered = xpst_config::Mapping::new();
        ordered.insert(Value::from("ok"), boolean(true));
        if let Value::Mapping(map) = destination.masked() {
            for (key, value) in map {
                ordered.insert(key, value);
            }
        }
        let _ = write_python_json(out, &Value::Mapping(ordered));
        return exit::SUCCESS;
    }
    let _ = write_text(
        out,
        &format!(
            "Stored {} ingest credentials ({})",
            row.display_name, destination.ingest_url
        ),
    );
    if row.ingest_class == "manual" {
        let _ = write_text(
            out,
            "This platform issues a per-session key — re-run this command before every stream.",
        );
    }
    exit::SUCCESS
}

/// `xpst stream clear-key PLATFORM`.
pub fn clear_key(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = json_mode(global, leaf);
    let (_, dir) = match resolve(global, out, err) {
        Ok(resolved) => resolved,
        Err(code) => return code,
    };
    let platform = leaf_string(leaf, "arg:platform").unwrap_or_default();
    let removed = CredentialStore::new(&dir)
        .delete(&format!("{KEY_PREFIX}{}", platform.trim().to_lowercase()));
    if json {
        let payload = object(vec![
            ("ok", boolean(removed)),
            ("platform", string(platform.clone())),
            ("removed", boolean(removed)),
        ]);
        let _ = write_python_json(out, &payload);
        return exit::SUCCESS;
    }
    let _ = write_text(
        out,
        &if removed {
            format!("Removed the stored {platform} ingest key.")
        } else {
            format!("No stored ingest key for {platform}.")
        },
    );
    exit::SUCCESS
}

/// `xpst stream relay-config`.
pub fn relay_config(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json = json_mode(global, leaf);
    let (_, dir) = match resolve(global, out, err) {
        Ok(resolved) => resolved,
        Err(code) => return code,
    };
    let registry = match registry() {
        Ok(registry) => registry,
        Err(error) => {
            let _ = writeln!(err, "xpst stream relay-config: {error}");
            return exit::GENERAL;
        }
    };
    let path_name =
        leaf_string(leaf, "opt:path-name").unwrap_or_else(|| DEFAULT_PATH_NAME.to_string());
    let rtmp_port =
        crate::clickargs::leaf_integer(leaf, "opt:rtmp-port").unwrap_or(DEFAULT_RTMP_PORT);
    let output = leaf_string(leaf, "opt:output").filter(|value| !value.is_empty());

    let built = load_destinations(&dir, &registry).and_then(|destinations| {
        if let Some(output) = &output {
            let config = build_relay_config(&destinations, &path_name, rtmp_port)?;
            let text = render_relay_config(&config);
            let target = crate::commands::state::expanduser(output);
            if let Some(parent) = target.parent() {
                if !parent.as_os_str().is_empty() {
                    std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
                }
            }
            crate::fsutil::write_private(&target, text.as_bytes())?;
            let mut summary = relay_summary(&destinations, &path_name, rtmp_port);
            if let Value::Mapping(map) = &mut summary {
                map.insert(Value::from("output"), string(target.display().to_string()));
            }
            return Ok((summary, None));
        }
        let config = build_relay_config(&destinations, &path_name, rtmp_port)?;
        let rendered = render_relay_config(&config);
        Ok((
            relay_summary(&destinations, &path_name, rtmp_port),
            Some(rendered),
        ))
    });

    match built {
        Ok((summary, rendered)) => {
            if json {
                let mut payload = summary;
                if let (Some(rendered), Value::Mapping(map)) = (rendered, &mut payload) {
                    map.insert(Value::from("config"), string(rendered));
                }
                let _ = write_python_json(out, &payload);
                return exit::SUCCESS;
            }
            match rendered {
                Some(rendered) => {
                    let _ = write_text(out, &rendered);
                }
                None => {
                    let target = summary
                        .get("output")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string();
                    let count = summary
                        .get("destination_count")
                        .and_then(Value::as_i64)
                        .unwrap_or_default();
                    let _ = write_text(
                        out,
                        &format!("Wrote the relay config to {target} ({count} destination(s), mode 0600)"),
                    );
                    let _ = write_text(
                        out,
                        &format!(
                            "Encoder: push RTMP to {}",
                            summary
                                .get("relay_ingest_url")
                                .and_then(Value::as_str)
                                .unwrap_or_default()
                        ),
                    );
                }
            }
            exit::SUCCESS
        }
        Err(error) => {
            if json {
                let payload = object(vec![
                    ("ok", boolean(false)),
                    ("error", string(error.clone())),
                ]);
                let _ = write_python_json(out, &payload);
            }
            let _ = write_text(out, &wrap_at(&error, WIDTH));
            exit::CONFIG_ERROR
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_registry_carries_every_ingest_path() {
        let registry = registry().expect("registry parses");
        assert_eq!(registry.paths.len(), 10);
        assert_eq!(registry.paths[0].platform, "youtube");
        assert_eq!(registry.registrations.len(), 2);
        assert_eq!(registry.refusals.len(), 2);
    }

    #[test]
    fn a_destination_refuses_a_non_rtmp_url() {
        let registry = registry().unwrap();
        let error = RelayDestination::new(&registry, "youtube", "https://x/y", "k").unwrap_err();
        assert!(error.contains("rtmp://"), "{error}");
    }

    #[test]
    fn a_destination_refuses_a_key_with_a_hash() {
        let registry = registry().unwrap();
        let error =
            RelayDestination::new(&registry, "youtube", "rtmps://a/live2", "k#1").unwrap_err();
        assert!(error.contains('#'), "{error}");
    }

    #[test]
    fn an_unsupported_platform_is_refused() {
        let registry = registry().unwrap();
        let error = RelayDestination::new(&registry, "x", "rtmp://a/b", "k").unwrap_err();
        assert!(error.contains("no live ingest path"), "{error}");
    }

    #[test]
    fn the_relay_config_is_the_documented_shape() {
        let registry = registry().unwrap();
        let destination =
            RelayDestination::new(&registry, "youtube", "rtmps://a.example/live2", "secret")
                .unwrap();
        let config = build_relay_config(&[destination], "xpst", 1935).unwrap();
        let rendered = render_relay_config(&config);
        assert!(rendered.starts_with("# Generated by xPST"));
        assert!(
            rendered.contains("rtmpAddress: ':1935'") || rendered.contains("rtmpAddress: :1935")
        );
        assert!(rendered.contains("dest: rtmps://a.example/live2#secret"));
        assert!(rendered.contains("rtsp: false"));
    }

    #[test]
    fn the_summary_never_carries_the_key() {
        let registry = registry().unwrap();
        let destination = RelayDestination::new(
            &registry,
            "youtube",
            "rtmps://a.example/live2",
            "secret-key",
        )
        .unwrap();
        let summary = relay_summary(&[destination], "xpst", 1935);
        let text = xpst_config::python_style_json(&summary).unwrap();
        assert!(!text.contains("secret-key"), "{text}");
        assert!(text.contains("se…ey"), "{text}");
    }

    #[test]
    fn an_empty_destination_list_is_refused() {
        let error = build_relay_config(&[], "xpst", 1935).unwrap_err();
        assert!(error.contains("no destinations configured"), "{error}");
    }
}
