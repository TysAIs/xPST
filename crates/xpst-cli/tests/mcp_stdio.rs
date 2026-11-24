//! The Rust MCP server speaks the Python server's wire.
//!
//! `xpst mcp start` is a stdio server: it never exits, so it cannot be a
//! `local_surface` row (that harness runs a command to completion and compares
//! its stdout). This test drives the real binary over stdin/stdout — the same
//! protocol an agent speaks — and holds every response to a capture from the
//! shipped Python server (`xtask/scripts/capture_mcp_stdio.py`).
//!
//! What is compared, and why:
//!
//! * `initialize` — the negotiated revision, the capabilities document and the
//!   server identity, verbatim. `capabilities.tools.listChanged` is `false` in
//!   both; rmcp's `enable_tools()` omits the field, which is a different wire
//!   document for the same capability.
//! * `tools/list` — the count, the ordered names and an FNV-1a digest of the
//!   parsed tool array with sorted keys. The digest is order-insensitive on
//!   purpose: the Python SDK re-serializes `inputSchema` in its own key order
//!   for the negotiated revision, so a byte comparison would pin the SDK's
//!   normalisation rather than the surface. The schema *content* is pinned by
//!   `crates/xpst-mcp/tests/fixtures/python_tool_registry.json`.
//! * `tools/call` — `isError` and the exact `content[0].text` body. Python
//!   serves `json.dumps` output there, so the body is the parity surface: a
//!   tool that renders its payload differently (sorted keys, compact
//!   separators) is a different document to an agent that parses the text.
//!
//! The three scenarios differ only in the environment, because the consent
//! guards are read from it: a mutating call is refused by default, refused
//! outright under `XPST_MCP_READONLY`, and refused without `"confirm": true`
//! under `XPST_MCP_REQUIRE_CONFIRM`. Every mutating call in the fixture is a
//! refusal — nothing here posts, uploads or mutates.
//!
//! Regenerate the fixture with:
//!
//! ```text
//! PYTHONPATH=src XPST_PYTHON=<venv>/bin/python python xtask/scripts/capture_mcp_stdio.py
//! ```

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::time::Duration;

use serde_json::{json, Value};

const FIXTURE: &str = include_str!("fixtures/mcp_stdio.json");

/// The fixture's scenario count. A loop over an emptied array asserts nothing.
const EXPECTED_SCENARIOS: usize = 3;

/// The state file every scenario starts from (an empty ledger).
const STATE: &str =
    r#"{"version":2,"posted_videos":{},"health":{"total_processed":0,"platforms":{}}}"#;

struct Server {
    child: Child,
    stdin: ChildStdin,
    lines: Receiver<String>,
}

impl Server {
    fn start(binary: &str, argv: &[&str], config_dir: &Path, env: &Value) -> Self {
        let mut command = Command::new(binary);
        command
            .args(argv)
            .env("XPST_CONFIG_DIR", config_dir)
            .env("HOME", config_dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        // Inherited `XPST_*` overrides would make the guards a property of the
        // developer's shell instead of of the fixture.
        for (name, _) in std::env::vars() {
            if name.starts_with("XPST_") {
                command.env_remove(&name);
            }
        }
        command
            .env("XPST_CONFIG_DIR", config_dir)
            .env("HOME", config_dir);
        if let Some(env) = env.as_object() {
            for (name, value) in env {
                command.env(name, value.as_str().unwrap_or_default());
            }
        }

        let mut child = command.spawn().expect("spawn the MCP server");
        let stdin = child.stdin.take().expect("stdin");
        let stdout = child.stdout.take().expect("stdout");
        let (tx, lines) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                match line {
                    Ok(line) => {
                        if tx.send(line).is_err() {
                            break;
                        }
                    }
                    Err(_) => break,
                }
            }
        });
        Self {
            child,
            stdin,
            lines,
        }
    }

    fn send(&mut self, message: Value) {
        let mut text = serde_json::to_string(&message).expect("serialise request");
        text.push('\n');
        self.stdin
            .write_all(text.as_bytes())
            .expect("write request to the server");
        self.stdin.flush().expect("flush");
    }

    fn receive(&self) -> Value {
        let line = self
            .lines
            .recv_timeout(Duration::from_secs(20))
            .expect("the server answered within 20s");
        serde_json::from_str(&line)
            .unwrap_or_else(|err| panic!("bad JSON from server: {err}: {line}"))
    }

    fn request(&mut self, id: u64, method: &str, params: Value) -> Value {
        self.send(json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}));
        loop {
            let message = self.receive();
            if message.get("id") == Some(&json!(id)) {
                return message;
            }
        }
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn binary() -> &'static str {
    env!("CARGO_BIN_EXE_xpst")
}

fn profile(name: &str) -> PathBuf {
    let dir = Path::new(env!("CARGO_TARGET_TMPDIR")).join(format!("mcp-stdio-{name}"));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("scratch profile");
    std::fs::write(dir.join("state.json"), STATE).expect("seed state");
    dir
}

/// FNV-1a 64-bit, the same digest `capture_mcp_stdio.py` records.
fn fnv1a64(text: &str) -> String {
    let mut value: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in text.as_bytes() {
        value ^= u64::from(*byte);
        value = value.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("{value:016x}")
}

/// `json.dumps(tools, sort_keys=True)` — the digest's input.
fn sorted_json(value: &Value) -> String {
    let mut sorted = value.clone();
    xpst_config::sort_keys_recursive(&mut sorted);
    xpst_config::python_style_json_compact(&sorted).expect("render tools")
}

fn scenarios() -> Vec<Value> {
    let document: Value = serde_json::from_str(FIXTURE).expect("fixture is valid JSON");
    let scenarios = document["scenarios"]
        .as_array()
        .expect("fixture has a scenarios array")
        .clone();
    assert_eq!(
        scenarios.len(),
        EXPECTED_SCENARIOS,
        "the fixture holds {} scenarios, not {EXPECTED_SCENARIOS}: the capture wrote an empty \
         or partial collection",
        scenarios.len()
    );
    scenarios
}

#[test]
fn the_rust_server_speaks_the_python_wire() {
    for scenario in scenarios() {
        let name = scenario["name"].as_str().expect("scenario name");
        let env = scenario["env"].clone();
        let dir = profile(name);
        let mut server = Server::start(binary(), &["mcp", "start"], &dir, &env);
        let root = dir.display().to_string();

        for step in scenario["steps"].as_array().expect("steps") {
            let id = step["id"].as_u64().expect("step id");
            let method = step["method"].as_str().expect("step method");
            match method {
                "initialize" => {
                    let response = server.request(
                        id,
                        "initialize",
                        json!({
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "xpst-parity-capture", "version": "0"},
                        }),
                    );
                    assert_eq!(
                        response["result"], step["result"],
                        "scenario `{name}`: initialize drifted"
                    );
                }
                "tools/list" => {
                    let response = server.request(id, "tools/list", json!({}));
                    let tools = response["result"]["tools"].as_array().expect("tools");
                    assert_eq!(
                        tools.len() as u64,
                        step["count"].as_u64().expect("count"),
                        "scenario `{name}`: tool count drifted"
                    );
                    let names: Vec<&str> = tools
                        .iter()
                        .map(|tool| tool["name"].as_str().unwrap_or_default())
                        .collect();
                    let expected: Vec<&str> = step["names"]
                        .as_array()
                        .expect("names")
                        .iter()
                        .map(|name| name.as_str().unwrap_or_default())
                        .collect();
                    assert_eq!(names, expected, "scenario `{name}`: tool order drifted");
                    assert_eq!(
                        fnv1a64(&sorted_json(&Value::Array(tools.clone()))),
                        step["digest"].as_str().expect("digest"),
                        "scenario `{name}`: the tool definitions drifted from the Python registry"
                    );
                }
                "tools/call" => {
                    let tool = step["tool"].as_str().expect("tool");
                    let arguments = if tool == "xpst_post" {
                        json!({
                            "media_path": "/nonexistent/parity.mp4",
                            "platforms": ["youtube"],
                            "caption": "parity fixture",
                        })
                    } else {
                        json!({})
                    };
                    let response = server.request(
                        id,
                        "tools/call",
                        json!({"name": tool, "arguments": arguments}),
                    );
                    let result = &response["result"];
                    assert_eq!(
                        result["isError"].as_bool().unwrap_or(false),
                        step["is_error"].as_bool().expect("is_error"),
                        "scenario `{name}`: {tool} error verdict drifted"
                    );
                    let text = result["content"][0]["text"]
                        .as_str()
                        .unwrap_or_default()
                        .replace(&root, "<PROFILE>");
                    assert_eq!(
                        text,
                        step["text"].as_str().expect("text"),
                        "scenario `{name}`: {tool} body drifted"
                    );
                }
                other => panic!("scenario `{name}`: unknown step method {other}"),
            }
        }
    }
}

/// A bare `xpst mcp` starts the server, because the shipped Click group is
/// `invoke_without_command=True` and `python -m xpst mcp` is the documented
/// stdio entry point. The Rust group used to print usage instead.
#[test]
fn a_bare_mcp_group_starts_the_server() {
    let dir = profile("bare");
    let mut server = Server::start(binary(), &["mcp"], &dir, &json!({}));
    let response = server.request(
        1,
        "initialize",
        json!({
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "bare-group", "version": "0"},
        }),
    );
    assert_eq!(response["result"]["serverInfo"]["name"], "xpst-mcp");
    assert_eq!(response["result"]["protocolVersion"], "2024-11-05");
}

/// The server exits cleanly when the client closes the pipe, so a supervisor
/// (the desktop shell, an agent host) can stop it without a signal.
#[test]
fn the_server_exits_zero_when_stdin_closes() {
    let dir = profile("exit");
    let mut child = Command::new(binary())
        .args(["mcp", "start"])
        .env("XPST_CONFIG_DIR", &dir)
        .env("HOME", &dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn the MCP server");
    drop(child.stdin.take());
    let status = child.wait().expect("the server exits");
    assert_eq!(status.code(), Some(0), "a closed stdin is a clean shutdown");
}
