//! Two-engine parity for the local, offline surface.
//!
//! Each scenario in `fixtures/local_surface.json` was produced by *running* the
//! shipped Python CLI against a scratch profile
//! (`xtask/scripts/capture_local_surface.py`). This test replays the same seed
//! files and arguments against the Rust binary and requires the same exit code
//! and the same parsed `--json` payload.
//!
//! Why a subprocess instead of an in-process call: the engine resolves its
//! profile from `XPST_CONFIG_DIR`/`HOME`, and a test process cannot set those
//! safely while other tests run in parallel. Running the real binary also
//! exercises the argument parsing and the first-run config materialization that
//! an in-process call would skip.
//!
//! Regenerate the fixture with:
//!
//! ```text
//! PYTHONPATH=src XPST_PYTHON=<venv>/bin/python python xtask/scripts/capture_local_surface.py
//! ```

use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::Value;

const FIXTURE: &str = include_str!("fixtures/local_surface.json");

/// The fixture's scenario count. A loop over an emptied array asserts nothing,
/// so the count is pinned here as well as in the capture script.
const EXPECTED_SCENARIOS: usize = 48;

fn document() -> Value {
    serde_json::from_str(FIXTURE).expect("fixture is valid JSON")
}

fn scenarios(document: &Value) -> &Vec<Value> {
    let scenarios = document
        .get("scenarios")
        .and_then(Value::as_array)
        .expect("fixture has a scenarios array");
    assert_eq!(
        scenarios.len(),
        EXPECTED_SCENARIOS,
        "the fixture holds {} scenarios, not {EXPECTED_SCENARIOS}: the capture wrote an \
         empty or partial collection",
        scenarios.len()
    );
    assert_eq!(
        document["scenario_count"].as_u64().unwrap_or(0) as usize,
        EXPECTED_SCENARIOS,
        "scenario_count disagrees with the checked-in scenarios"
    );
    scenarios
}

fn binary() -> &'static str {
    env!("CARGO_BIN_EXE_xpst")
}

struct Profile {
    dir: PathBuf,
}

impl Profile {
    fn new(name: &str) -> Self {
        let dir =
            std::env::temp_dir().join(format!("xpst-local-surface-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("scratch profile");
        Self { dir }
    }

    fn seed(&self, seed: &Value) {
        let Some(files) = seed.as_object() else {
            return;
        };
        for (relative, content) in files {
            let path = self.dir.join(relative);
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent).expect("seed directory");
            }
            std::fs::write(&path, content.as_str().expect("seed is text")).expect("seed file");
        }
    }

    fn path(&self) -> &Path {
        &self.dir
    }
}

impl Drop for Profile {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// The profile directory both engines are pointed at.
///
/// A scenario may pin it to a subdirectory (`config_dir`): the config write
/// commands need that, because Python resolves `~/.xpst/config.yaml` from HOME
/// (and `config import` hardcodes it) while the Rust engine resolves
/// `XPST_CONFIG_DIR`. HOME=profile + XPST_CONFIG_DIR=profile/.xpst makes both
/// engines touch the same file.
fn config_dir_for(row: &Value, profile: &Profile) -> std::path::PathBuf {
    match row.get("config_dir").and_then(Value::as_str) {
        Some(suffix) if !suffix.is_empty() => profile.path().join(suffix),
        _ => profile.path().to_path_buf(),
    }
}

fn run(
    binary: &str,
    profile: &Profile,
    argv: &[String],
    env: &[(String, String)],
    config_dir: &std::path::Path,
) -> (i32, String, String) {
    let mut command = Command::new(binary);
    command.args(argv).env("XPST_CONFIG_DIR", config_dir);
    // The capture strips inherited `XPST_*` overrides so the fixture is
    // machine-independent; the replay must do the same or a developer with
    // `XPST_X_AUTH_MODE` exported would see a spurious failure.
    for (name, _) in std::env::vars() {
        if name.starts_with("XPST_") {
            command.env_remove(&name);
        }
    }
    command.env("XPST_CONFIG_DIR", config_dir);
    for (name, value) in env {
        command.env(
            name,
            value.replace("PROFILE", &profile.path().display().to_string()),
        );
    }
    let output = command.output().expect("the rust binary runs");
    (
        output.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&output.stdout).to_string(),
        String::from_utf8_lossy(&output.stderr).to_string(),
    )
}

/// The scenario's extra environment, with `PROFILE` substituted.
fn env_for(row: &Value, profile: &Profile) -> Vec<(String, String)> {
    let mut env: Vec<(String, String)> = row
        .get("env")
        .and_then(Value::as_object)
        .map(|map| {
            map.iter()
                .map(|(name, value)| {
                    (
                        name.clone(),
                        value
                            .as_str()
                            .unwrap_or_default()
                            .replace("PROFILE", &profile.path().display().to_string()),
                    )
                })
                .collect()
        })
        .unwrap_or_default();
    if row.get("config_dir").and_then(Value::as_str).is_some() {
        env.push(("HOME".to_string(), profile.path().display().to_string()));
    }
    env
}

/// Replace the scratch profile's absolute path with `<PROFILE>` everywhere in a
/// payload, and blank the wall-clock fields the fixture masks.
fn normalize(value: &mut Value, profile: &Profile, mask: &[String]) {
    let root = profile.path().display().to_string();
    fn walk(value: &mut Value, root: &str) {
        match value {
            Value::String(text) => {
                if text.contains(root) {
                    *text = text.replace(root, "<PROFILE>");
                }
            }
            Value::Array(items) => items.iter_mut().for_each(|item| walk(item, root)),
            Value::Object(map) => map.values_mut().for_each(|item| walk(item, root)),
            _ => {}
        }
    }
    walk(value, &root);
    if let Value::Object(map) = value {
        for path in mask {
            if map.contains_key(path) {
                map.insert(path.clone(), Value::String("<TS>".to_string()));
            }
        }
    }
}

/// FNV-1a 64-bit, the same digest `capture_local_surface.py` records.
///
/// Dependency-free on purpose: this is a regression guard, not a security
/// boundary, and adding a hashing crate to the workspace for a test would be a
/// dependency decision the card does not need.
fn fnv1a64(text: &str) -> String {
    let mut value: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in text.as_bytes() {
        value ^= u64::from(*byte);
        value = value.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("{value:016x}")
}

fn mask_for(row: &Value) -> Vec<String> {
    row.get("mask")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

fn argv_for(row: &Value, profile: &Profile) -> Vec<String> {
    let mut argv: Vec<String> = row["argv"]
        .as_array()
        .expect("argv is an array")
        .iter()
        .map(|argument| {
            argument.as_str().unwrap_or_default().replace(
                "MISSING",
                &profile
                    .path()
                    .join("does-not-exist.yaml")
                    .display()
                    .to_string(),
            )
        })
        .collect();
    if let Some(substitutions) = row.get("substitutions").and_then(Value::as_object) {
        for (token, relative) in substitutions {
            let replacement = profile
                .path()
                .join(relative.as_str().unwrap_or_default())
                .display()
                .to_string();
            argv = argv
                .into_iter()
                .map(|argument| argument.replace(token, &replacement))
                .collect();
        }
    }
    // `PROFILE` is the scratch profile itself: a scenario that writes a file
    // names it inside the profile so the run stays hermetic.
    let root = profile.path().display().to_string();
    argv.into_iter()
        .map(|argument| argument.replace("PROFILE", &root))
        .collect()
}

/// Compare the payload, allowing for the two values the fixture cannot pin:
/// a snapshot's absolute path and its machine-local mtime.
fn assert_payload_matches(expected: &Value, actual: &Value, scenario: &str) {
    let (mut expected, mut actual) = (expected.clone(), actual.clone());
    if let Some(snapshots) = expected
        .get_mut("paused_snapshots")
        .and_then(Value::as_array_mut)
    {
        for snapshot in snapshots.iter_mut() {
            if let Some(name) = snapshot.get("file").and_then(Value::as_str) {
                snapshot["file"] = Value::String(name.to_string());
            }
        }
    }
    if let Some(snapshots) = actual
        .get_mut("paused_snapshots")
        .and_then(Value::as_array_mut)
    {
        for snapshot in snapshots.iter_mut() {
            let Some(path) = snapshot.get("file").and_then(Value::as_str) else {
                continue;
            };
            let name = Path::new(path)
                .file_name()
                .map(|name| name.to_string_lossy().to_string())
                .unwrap_or_default();
            snapshot["file"] = Value::String(name);
            let Some(stamp) = snapshot.get("modified_at").and_then(Value::as_str) else {
                panic!("{scenario}: snapshot modified_at must be a rendered timestamp");
            };
            assert!(
                xpst_state::status::parse_instant(stamp).is_some(),
                "{scenario}: modified_at `{stamp}` is not a parseable local timestamp"
            );
            snapshot.as_object_mut().unwrap().remove("modified_at");
        }
    }
    if let Some(snapshots) = expected
        .get_mut("paused_snapshots")
        .and_then(Value::as_array_mut)
    {
        for snapshot in snapshots.iter_mut() {
            snapshot.as_object_mut().unwrap().remove("modified_at");
        }
    }
    assert_eq!(actual, expected, "scenario `{scenario}` payload drifted");
}

/// Compare the files a scenario wrote, byte for byte (after the same
/// `<PROFILE>` normalisation the payload gets).
fn assert_files_match(row: &Value, profile: &Profile, scenario: &str) {
    let Some(expected) = row.get("files").and_then(Value::as_object) else {
        return;
    };
    assert!(
        !expected.is_empty(),
        "scenario `{scenario}` records an empty files map"
    );
    for (relative, want) in expected {
        let path = profile.path().join(relative);
        let actual = std::fs::read_to_string(&path)
            .unwrap_or_else(|error| panic!("scenario `{scenario}`: {relative} unreadable: {error}"))
            .replace(&profile.path().display().to_string(), "<PROFILE>");
        assert_eq!(
            actual,
            want.as_str().unwrap_or_default(),
            "scenario `{scenario}`: {relative} differs from the Python engine"
        );
    }
}

#[test]
fn every_recorded_scenario_matches_the_python_engine() {
    let document = document();
    let mut checked = 0;
    for row in scenarios(&document) {
        let name = row["name"].as_str().expect("scenario name");
        let profile = Profile::new(name);
        profile.seed(&row["seed"]);
        let argv = argv_for(row, &profile);
        let env = env_for(row, &profile);
        let config_dir = config_dir_for(row, &profile);
        let (code, stdout, stderr) = run(binary(), &profile, &argv, &env, &config_dir);
        let compare = row["compare"].as_str().expect("compare mode");

        match compare {
            "json" => {
                assert_eq!(
                    code,
                    row["exit_code"].as_i64().unwrap_or(-1) as i32,
                    "scenario `{name}` exit code drifted: {stderr}"
                );
                let mut actual: Value =
                    serde_json::from_str(stdout.trim()).unwrap_or_else(|error| {
                        panic!(
                            "scenario `{name}` did not print JSON ({error}):\n{stdout}\n{stderr}"
                        )
                    });
                normalize(&mut actual, &profile, &mask_for(row));
                assert_payload_matches(&row["stdout_json"], &actual, name);
                assert_files_match(row, &profile, name);
            }
            "stdout" => {
                assert_eq!(
                    code,
                    row["exit_code"].as_i64().unwrap_or(-1) as i32,
                    "scenario `{name}` exit code drifted: {stderr}"
                );
                let expected = row["stdout"]
                    .as_str()
                    .expect("stdout scenarios record the text");
                let actual = stdout.replace(&profile.path().display().to_string(), "<PROFILE>");
                assert_eq!(actual, expected, "scenario `{name}` stdout drifted");
            }
            "digest" => {
                // `providers` prints ~54 kB; the fixture pins a digest of the
                // exact stdout instead of the document. Any byte change fails,
                // and `capture_local_surface.py --check-with` prints the real
                // diff in CI.
                assert_eq!(
                    code,
                    row["exit_code"].as_i64().unwrap_or(-1) as i32,
                    "scenario `{name}` exit code drifted: {stderr}"
                );
                let actual = stdout.replace(&profile.path().display().to_string(), "<PROFILE>");
                let expected = row["stdout_sha256"]
                    .as_str()
                    .expect("digest scenarios record the digest");
                assert_eq!(
                    fnv1a64(&actual),
                    expected,
                    "scenario `{name}` stdout drifted ({} bytes, expected {}):\n{}",
                    actual.len(),
                    row["stdout_bytes"].as_u64().unwrap_or(0),
                    actual.chars().take(400).collect::<String>()
                );
                assert_eq!(
                    actual.len() as u64,
                    row["stdout_bytes"].as_u64().unwrap_or(0),
                    "scenario `{name}` stdout length drifted"
                );
            }
            "exit" => {
                assert_eq!(
                    code,
                    row["exit_code"].as_i64().unwrap_or(-1) as i32,
                    "scenario `{name}` exit code drifted: {stderr}"
                );
            }
            "divergence" => {
                let expected = row["rust_exit_code"]
                    .as_i64()
                    .expect("divergence rows name the rust exit code");
                assert_eq!(
                    code, expected as i32,
                    "scenario `{name}` divergence drifted"
                );
                assert!(
                    !row["why"].as_str().unwrap_or("").trim().is_empty(),
                    "scenario `{name}` is a divergence with no recorded reason"
                );
                assert_ne!(
                    row["exit_code"].as_i64(),
                    row["rust_exit_code"].as_i64(),
                    "scenario `{name}` is recorded as a divergence but both engines now agree — \
                     drop the row or the note is stale"
                );
            }
            other => panic!("scenario `{name}` has unknown compare mode `{other}`"),
        }
        checked += 1;
    }
    assert_eq!(checked, EXPECTED_SCENARIOS);
}

#[test]
fn a_fresh_profile_is_created_with_the_same_config_the_python_engine_writes() {
    // The first-run materialization is part of `status`'s behaviour: Python
    // creates the default config rather than failing, and a QA lane that ran
    // only the Rust engine then looked at the profile must find the same file.
    let profile = Profile::new("fresh-config");
    let (code, _stdout, stderr) = run(
        binary(),
        &profile,
        &["status".to_string(), "--json".to_string()],
        &[],
        profile.path(),
    );
    assert_eq!(code, 0, "status must not fail on a fresh profile: {stderr}");
    let written = std::fs::read_to_string(profile.path().join("config.yaml"))
        .expect("status created the first-run config");
    assert_eq!(
        written,
        xpst_cli::commands::bootstrap::DEFAULT_CONFIG_YAML,
        "the created config is not the Python engine's default document"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = std::fs::metadata(profile.path().join("config.yaml"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(
            mode, 0o600,
            "the config holds credentials; keep it owner-only"
        );
    }
}

#[test]
fn schedule_list_never_writes_into_the_profile() {
    // Python's manager creates `.schedule.lock` and the directory. The Rust
    // reader is read-only on purpose; a listing must not mutate a paused
    // install's files.
    let profile = Profile::new("schedule-readonly");
    profile.seed(&serde_json::json!({
        "schedule.json": "[{\"id\":\"a\",\"scheduled_time\":\"2026-01-15T09:30:00+00:00\"}]"
    }));
    let before = std::fs::read_dir(profile.path())
        .unwrap()
        .flatten()
        .map(|entry| entry.file_name().to_string_lossy().to_string())
        .collect::<Vec<_>>();
    let (code, _stdout, stderr) = run(
        binary(),
        &profile,
        &[
            "schedule".to_string(),
            "list".to_string(),
            "--json".to_string(),
        ],
        &[],
        profile.path(),
    );
    assert_eq!(code, 0, "{stderr}");
    let after = std::fs::read_dir(profile.path())
        .unwrap()
        .flatten()
        .map(|entry| entry.file_name().to_string_lossy().to_string())
        .collect::<Vec<_>>();
    assert_eq!(before, after, "schedule list wrote into the profile");
}
