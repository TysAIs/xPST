//! The Rust `net_guard` port against the captured Python contract.
//!
//! `tests/fixtures/python_net_guard.json` is recorded from the shipped
//! `src/xpst/utils/net_guard.py::validate_public_url` by
//! `xtask/scripts/capture_net_guard.py`. Every row is replayed here through a
//! `StubResolver` built from the row's own `dns` map, so both engines see the
//! same answers and neither opens a socket.
//!
//! Two things are asserted per row, and the second is the one that catches a
//! port that "works" for the wrong reason:
//!
//! * the verdict — the returned URL, or the exact `BlockedURLError` sentence;
//! * `resolved` — the exact sequence of hosts the guard asked the resolver for.
//!   A port that resolves a literal IP, or resolves when `resolve=False`, or
//!   resolves before the `allow_hosts` escape hatch, produces the same verdict
//!   on most rows and a different sequence here.

use std::cell::RefCell;
use std::collections::BTreeMap;
use std::net::IpAddr;

use serde_json::Value;
use xpst_surface::net_guard::{validate_public_url, HostResolver, ResolveFailure, UrlPolicy};

const FIXTURE: &str = include_str!("fixtures/python_net_guard.json");

/// A loop over an emptied array asserts nothing, so the row count is pinned.
const EXPECTED_ROWS: usize = 235;

struct StubResolver {
    answers: BTreeMap<String, Option<Vec<IpAddr>>>,
    asked: RefCell<Vec<String>>,
}

impl HostResolver for StubResolver {
    fn resolve(&self, host: &str) -> Result<Vec<IpAddr>, ResolveFailure> {
        self.asked.borrow_mut().push(host.to_string());
        match self.answers.get(host) {
            Some(Some(addresses)) => Ok(addresses.clone()),
            Some(None) => Err(ResolveFailure::NotFound),
            None => panic!("unstubbed DNS lookup for {host:?}: the row must declare it in `dns`"),
        }
    }
}

fn policy(options: &Value) -> UrlPolicy {
    let strings = |value: &Value| -> Vec<String> {
        value
            .as_array()
            .expect("an options array")
            .iter()
            .map(|item| item.as_str().expect("a string").to_string())
            .collect()
    };
    UrlPolicy {
        allowed_schemes: strings(&options["allowed_schemes"]),
        allow_hosts: strings(&options["allow_hosts"]),
        allow_private: options["allow_private"].as_bool().expect("allow_private"),
        allow_ports: options["allow_ports"].as_array().map(|ports| {
            ports
                .iter()
                .map(|port| port.as_u64().expect("a port") as u16)
                .collect()
        }),
        resolve: options["resolve"].as_bool().expect("resolve"),
    }
}

fn resolver(dns: &Value) -> StubResolver {
    let mut answers = BTreeMap::new();
    for (host, value) in dns.as_object().expect("a dns map") {
        let addresses = if value.is_null() {
            None
        } else {
            Some(
                value
                    .as_array()
                    .expect("an address list")
                    .iter()
                    .map(|address| {
                        address
                            .as_str()
                            .expect("an address")
                            .parse::<IpAddr>()
                            .expect("a parseable address")
                    })
                    .collect(),
            )
        };
        answers.insert(host.clone(), addresses);
    }
    StubResolver {
        answers,
        asked: RefCell::new(Vec::new()),
    }
}

#[test]
fn every_captured_row_matches() {
    let document: Value = serde_json::from_str(FIXTURE).expect("the fixture is valid JSON");
    let rows = document["rows"].as_array().expect("a rows array");
    assert_eq!(
        rows.len(),
        EXPECTED_ROWS,
        "the fixture holds {} rows, not {EXPECTED_ROWS}: the capture wrote an empty or partial \
         collection",
        rows.len()
    );

    let mut failures = Vec::new();
    for row in rows {
        let label = row["label"].as_str().expect("a label");
        let url = row["url"].as_str().expect("a url");
        let stub = resolver(&row["dns"]);
        let outcome = validate_public_url(url, &policy(&row["options"]), &stub);

        let expected = &row["outcome"];
        let actual = match &outcome {
            Ok(value) => serde_json::json!({"ok": true, "value": value}),
            Err(error) => serde_json::json!({"ok": false, "error": error.reason}),
        };
        if actual != *expected {
            failures.push(format!(
                "{label}: expected {expected}, got {actual} (url {url:?})"
            ));
        }

        let asked: Vec<String> = stub.asked.borrow().clone();
        let expected_asked: Vec<String> = row["resolved"]
            .as_array()
            .expect("a resolved array")
            .iter()
            .map(|host| host.as_str().expect("a host").to_string())
            .collect();
        if asked != expected_asked {
            failures.push(format!(
                "{label}: resolved {asked:?}, Python resolved {expected_asked:?}"
            ));
        }
    }

    assert!(
        failures.is_empty(),
        "{} of {} rows diverged from the Python contract:\n{}",
        failures.len(),
        rows.len(),
        failures.join("\n")
    );
}

/// The capture must stay offline: every host the guard resolves has a declared
/// answer, so no row can fall through to the real resolver.
///
/// The reverse is deliberately NOT asserted: a row may declare an answer it
/// never uses (`allow-hosts-case-insensitive` declares the private address the
/// escape hatch returns before), and that is a fact about the guard worth
/// keeping in the fixture.
#[test]
fn the_capture_is_self_contained() {
    let document: Value = serde_json::from_str(FIXTURE).expect("the fixture is valid JSON");
    for row in document["rows"].as_array().expect("a rows array") {
        let label = row["label"].as_str().expect("a label");
        let declared: Vec<String> = row["dns"]
            .as_object()
            .expect("a dns map")
            .keys()
            .cloned()
            .collect();
        let asked: Vec<String> = row["resolved"]
            .as_array()
            .expect("a resolved array")
            .iter()
            .map(|host| host.as_str().expect("a host").to_string())
            .collect();
        for host in &asked {
            assert!(
                declared.contains(host),
                "{label}: resolved {host:?} without declaring it in `dns`"
            );
        }
    }
}
