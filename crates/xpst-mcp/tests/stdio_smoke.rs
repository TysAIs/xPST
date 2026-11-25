//! End-to-end stdio check: the binary really speaks MCP.
//!
//! The registry tests prove the surface's contents; this proves the transport.
//! It drives the real `xpst-mcp` binary over stdin/stdout with newline-delimited
//! JSON-RPC — initialize, tools/list, and calls that must succeed, be refused by
//! the consent guard, and be reported as not implemented.

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::time::Duration;

use serde_json::{json, Value};

struct Server {
    child: Child,
    stdin: ChildStdin,
    lines: Receiver<String>,
}

impl Server {
    fn start(config_dir: &Path) -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_xpst-mcp"))
            .env("XPST_CONFIG_DIR", config_dir)
            .env_remove("XPST_MCP_READONLY")
            .env_remove("XPST_MCP_ALLOW_MUTATIONS")
            .env_remove("XPST_MCP_REQUIRE_CONFIRM")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn xpst-mcp");
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

    /// Send a notification (no id, no response).
    fn notify(&mut self, method: &str) {
        self.send(json!({"jsonrpc": "2.0", "method": method}));
    }

    fn receive(&self) -> Value {
        let line = self
            .lines
            .recv_timeout(Duration::from_secs(10))
            .expect("the server answered within 10s");
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

fn config_dir(name: &str) -> PathBuf {
    let dir = Path::new(env!("CARGO_TARGET_TMPDIR")).join(name);
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("create profile");
    dir
}

#[test]
fn the_binary_serves_the_registry_over_stdio() {
    let dir = config_dir("stdio_smoke");
    std::fs::write(
        dir.join("state.json"),
        r#"{"version":2,"posted_videos":{"a":{"posted_to":{"youtube":{}}}},"health":{"total_processed":1,"platforms":{}}}"#,
    )
    .expect("write state");

    let mut server = Server::start(&dir);

    let init = server.request(
        1,
        "initialize",
        json!({
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "xpst-smoke", "version": "0"}
        }),
    );
    assert_eq!(init["result"]["serverInfo"]["name"], json!("xpst-mcp"));
    assert_ne!(init["result"]["capabilities"]["tools"], Value::Null);

    server.send(json!({"jsonrpc": "2.0", "method": "notifications/initialized"}));

    let listed = server.request(2, "tools/list", json!({}));
    let tools = listed["result"]["tools"].as_array().expect("tools array");
    assert_eq!(tools.len(), 46);
    assert_eq!(tools[0]["name"], json!("xpst_run"));
    assert!(
        tools
            .iter()
            .all(|tool| tool["inputSchema"]["additionalProperties"] == json!(false)),
        "every advertised schema must forbid unknown arguments"
    );

    let status = server.request(
        3,
        "tools/call",
        json!({"name": "xpst_status", "arguments": {}}),
    );
    assert_eq!(status["result"]["isError"], json!(false));
    let text = status["result"]["content"][0]["text"]
        .as_str()
        .expect("text content");
    let payload: Value = serde_json::from_str(text).expect("payload is JSON");
    assert_eq!(payload["total_videos_tracked"], json!(1));

    // No env opt-in: publishing must be refused, with the Python wording.
    let blocked = server.request(
        4,
        "tools/call",
        json!({"name": "xpst_post", "arguments": {"video_path": "/tmp/x.mp4"}}),
    );
    assert_eq!(blocked["result"]["isError"], json!(true));
    let text = blocked["result"]["content"][0]["text"]
        .as_str()
        .expect("text content");
    // The body is the Python sentence, not a JSON envelope: an agent that
    // already handles the shipped server's refusal handles this one unchanged.
    assert!(
        text.contains("xpst_post posts to or mutates REAL accounts."),
        "{text}"
    );
    assert!(text.contains("XPST_MCP_ALLOW_MUTATIONS=1"), "{text}");

    let stubbed = server.request(
        5,
        "tools/call",
        json!({"name": "xpst_analytics", "arguments": {}}),
    );
    assert_eq!(stubbed["result"]["isError"], json!(true));
    let text = stubbed["result"]["content"][0]["text"]
        .as_str()
        .expect("text content");
    assert!(text.contains("is not implemented yet"), "{text}");
}

#[test]
fn a_read_only_server_still_refuses_mutations_when_allowed() {
    let dir = config_dir("stdio_readonly");
    let mut child = Command::new(env!("CARGO_BIN_EXE_xpst-mcp"))
        .env("XPST_CONFIG_DIR", &dir)
        .env("XPST_MCP_READONLY", "1")
        .env("XPST_MCP_ALLOW_MUTATIONS", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn");
    let mut stdin = child.stdin.take().expect("stdin");
    let stdout = child.stdout.take().expect("stdout");
    let (tx, lines) = mpsc::channel();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            if tx.send(line).is_err() {
                break;
            }
        }
    });

    let send = |stdin: &mut ChildStdin, message: Value| {
        let mut text = serde_json::to_string(&message).expect("serialise");
        text.push('\n');
        stdin.write_all(text.as_bytes()).expect("write");
        stdin.flush().expect("flush");
    };

    send(
        &mut stdin,
        json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "xpst-smoke", "version": "0"}}}),
    );
    let _ = lines
        .recv_timeout(Duration::from_secs(10))
        .expect("initialize");
    send(
        &mut stdin,
        json!({"jsonrpc": "2.0", "method": "notifications/initialized"}),
    );
    send(
        &mut stdin,
        json!({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "xpst_run", "arguments": {}}}),
    );
    let response: Value = loop {
        let line = lines.recv_timeout(Duration::from_secs(10)).expect("answer");
        let message: Value = serde_json::from_str(&line).expect("json");
        if message.get("id") == Some(&json!(2)) {
            break message;
        }
    };
    let text = response["result"]["content"][0]["text"]
        .as_str()
        .expect("text");
    assert!(text.contains("XPST_MCP_READONLY"), "{text}");

    let _ = child.kill();
    let _ = child.wait();
}

/// The negotiated protocol version is the one thing about this server that a
/// client cannot discover from `tools/list`, so it gets its own test.
///
/// The declared set is exactly the revisions whose lifecycle is an `initialize`
/// handshake. A client asking for one of them gets that revision echoed (no
/// silent downgrade); a client asking for a newer, inline-lifecycle revision is
/// answered with the newest handshake revision, which is what the MCP spec
/// requires of a server that cannot serve the requested version.
#[test]
fn protocol_versions_negotiate_to_the_declared_handshake_set() {
    use xpst_mcp::SUPPORTED_PROTOCOL_VERSIONS;

    let declared: Vec<&str> = SUPPORTED_PROTOCOL_VERSIONS
        .iter()
        .map(|v| v.as_str())
        .collect();
    assert_eq!(
        declared,
        vec!["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"],
        "the advertised protocol set changed — that is a product decision, not a refactor"
    );
    assert!(
        SUPPORTED_PROTOCOL_VERSIONS
            .iter()
            .all(|version| version.has_initialize()),
        "every advertised revision must be one this server can actually serve over `initialize`"
    );

    let dir = config_dir("stdio_protocol_versions");
    let cases = declared
        .iter()
        .map(|version| (*version, *version))
        // Newer than anything declared: the SDK falls back to the newest
        // revision that still has the handshake this server implements.
        .chain(std::iter::once(("2026-07-28", "2025-11-25")));
    let mut surface: Option<Vec<Value>> = None;

    for (requested, expected) in cases {
        let mut server = Server::start(&dir);
        let init = server.request(
            1,
            "initialize",
            json!({
                "protocolVersion": requested,
                "capabilities": {},
                "clientInfo": {"name": "xpst-protocol", "version": "0"}
            }),
        );
        assert_eq!(
            init["result"]["protocolVersion"],
            json!(expected),
            "requesting {requested} must negotiate {expected}, got: {init}"
        );
        assert_eq!(init["result"]["serverInfo"]["name"], json!("xpst-mcp"));

        server.notify("notifications/initialized");
        let listed = server.request(2, "tools/list", json!({}));
        let names: Vec<Value> = listed["result"]["tools"]
            .as_array()
            .unwrap_or_else(|| panic!("tools/list must return an array, got: {listed}"))
            .iter()
            .map(|tool| tool["name"].clone())
            .collect();
        assert_eq!(
            names.len(),
            46,
            "the tool surface must keep all 46 tools on every negotiated revision ({requested})"
        );
        // Identity, not just the count: a build that dropped one tool and added
        // another would keep the length and change what an agent can call.
        match &surface {
            None => surface = Some(names),
            Some(expected) => assert_eq!(
                &names, expected,
                "the tool surface must be identical on every negotiated revision ({requested})"
            ),
        }
    }
}

/// A client that speaks only the newest, handshake-less generation never sends
/// `initialize`: it puts the protocol version in each request's `_meta`. This
/// server does not claim that generation, so the request must be refused with
/// the spec's `UNSUPPORTED_PROTOCOL_VERSION` and the list it *can* serve —
/// never silence, which is what 0.11.0 did (no answer at all, no error).
#[test]
fn an_inline_lifecycle_only_client_gets_a_typed_refusal_not_silence() {
    let dir = config_dir("stdio_modern_client");
    let mut server = Server::start(&dir);
    let declared: Vec<&str> = xpst_mcp::SUPPORTED_PROTOCOL_VERSIONS
        .iter()
        .map(|version| version.as_str())
        .collect();
    let meta = json!({
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "modern", "version": "0"},
        "io.modelcontextprotocol/clientCapabilities": {}
    });

    for (id, method) in [(1_u64, "discover"), (2, "tools/list")] {
        let response = server.request(id, method, json!({"_meta": meta}));
        assert_eq!(
            response["error"]["code"],
            json!(-32022),
            "{method} without a handshake must be refused with UNSUPPORTED_PROTOCOL_VERSION, got: {response}"
        );
        let supported: Vec<&str> = response["error"]["data"]["supported"]
            .as_array()
            .unwrap_or_else(|| panic!("the refusal must name what is supported: {response}"))
            .iter()
            .map(|version| {
                version
                    .as_str()
                    .unwrap_or_else(|| panic!("supported entries must be strings: {response}"))
            })
            .collect();
        assert_eq!(
            supported, declared,
            "the refusal must list exactly the declared set, in order: {response}"
        );
    }
}
