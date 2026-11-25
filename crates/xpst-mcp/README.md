# xpst-mcp

The MCP server: rmcp on stdio, serving the xPST tool surface.

The Python engine in `../../src/xpst/mcp/server.py` is the contract. This crate
is being built up to serve the same 46 tool names with the same input schemas
and the same semantics.

## The rule this crate exists to keep

> **The Rust surface may never claim a tool it does not implement.**

That is a structural property here, not a review checklist:

- Each tool row states its implementation: `Implementation::Ported(handler)` or
  `Implementation::Stub { reason }`. There is no third state.
- A stub can only return a typed failure —
  `{"ok": false, "error": "not_implemented", "tool": "...", "detail": "..."}`
  with `isError: true`. There is no code path from a stub to a success payload,
  so a client can never mistake "not ported yet" for "did nothing to do".
- Tool names and input schemas are **not written by hand**. They are captured
  from the Python registry into `tests/fixtures/python_tool_registry.json`, and
  the registry refuses to build if a row has no Python definition.

## Ported tools (read-only, no network)

| Tool | Reads | Notes |
|---|---|---|
| `xpst_config_show` | `$XPST_CONFIG_DIR/config.yaml` | Secrets masked by the ported masker (`xpst-config`). Returns the Python projection (`accounts.*`, `video`, `monitoring`, `schedule`). |
| `xpst_status` | `state.json` | `StateManager.get_statistics()` field-for-field, including the `historical`/`platform_health_note` honesty markers. |
| `xpst_rate_limit_status` | `rate_cooldowns.json` | `cooldown_report()`: idle records omitted, same duration strings, same fixed sentence. |
| `xpst_schedule_list` | `schedule.json`, `schedule.paused.json`, `schedule.json.paused-*` | Sorted entries with `scheduled_time_local`, pause state, and the snapshot note that stops an empty queue reading as "deleted". |
| `xpst_providers` | captured provider catalog | Provider metadata only — no credentials, no network, no per-install state. |
| `xpst_readiness` | `config.yaml`, credential store, helper binaries | `build_readiness_report()`: the same checks, summary and role rows the CLI prints, wrapped in `{ok, contract_version, readiness}`. Compact render, as Python's handler does. |
| `xpst_security_audit` | `config.yaml`, credential file modes, env | `run_security_checks()`: every check reports what was observed; `dashboard_localhost` is `unobserved` because a CLI process never binds the socket. |
| `xpst_best_time` | `analytics.db` | `BestTimeAnalyzer`: `{platform, best_time}` for one platform, `{best_times}` for all. |
| `kb_query` | `$XPST_HOME/knowledge/<workspace>/nuggets.json` | `query_nuggets()` on the substring path (no embedding stack), `k=8` as the handler defaults. |
| `xpst_search` | `$XPST_HOME/knowledge/default/nuggets.json` | The top-level alias: `default` workspace, `k` from `limit` (default 10). |
| `xpst_bio_get` | `config.yaml` | `collect_links()`: enabled accounts with a usable handle, then the http(s) custom links. |
| `xpst_capabilities` | captured provider catalog + content contract | `canonical_provider_catalog()` projected to `name`/`display_name`/`roles`/`capabilities`/`state`/`docs_url`, plus `capability_document()` verbatim. Offline: every live-derived fact is `null`. |
| `xpst_followers` | `analytics.db` | `latest_followers()` + `follower_history(limit=7)`: per-platform count, growth and history, oldest first. |
| `xpst_activity` | `state.json` | `posted_videos.<id>.errors.<platform>` flattened to one row per (video, platform) with the `retry`/`review` action. Compact render, as Python's handler does. |
| `kb_areas` | `$XPST_HOME/knowledge/<workspace>/areas.json` | `kb_areas()`: areas in course order with `order` (1-based) and `nugget_count`. |
| `xpst_transcript` | `$XPST_HOME/knowledge/default/transcripts/` | `get_transcript()`: direct id, then `content:<id>`, then a filename substring match. A miss is a payload, an invalid id is an error result. |

Sixteen ported, thirty stubbed. Nothing is half-implemented: a tool is either
answering from a real local store or saying `not_implemented` with the reason.

## Stubbed tools

Each one is listed in `registry.rs` with the reason it is not ported. The
common reasons are: it needs platform credentials or the network, it reads a
store that is not ported (analytics/SQLite, knowledge vector store, activity
ledger), or it writes through an engine service that does not exist in Rust yet.

| Tool | Why it is a stub |
|---|---|
| `xpst_run` | runs the posting pipeline, which is not ported |
| `xpst_post` | publishes through provider adapters, which are not ported |
| `xpst_analytics` | reads analytics.db and can refresh from platform APIs |
| `xpst_cross_post_analytics` | reads analytics.db, which is not ported |
| `xpst_suggest_caption` | calls the AI caption service |
| `xpst_generate_ideas` | calls the AI content service |
| `xpst_schedule_add` | writes the schedule store through Python services, which are not ported |
| `xpst_schedule_cancel` | mutates the schedule store through Python services, which are not ported |
| `xpst_schedule_pause` | writes the pause marker through Python services, which are not ported |
| `xpst_schedule_resume` | releases the pause marker through Python services, which are not ported |
| `xpst_failures_retry` | re-uploads through provider adapters, which are not ported |
| `xpst_health` | probes every platform over the network |
| `xpst_backfill` | re-posts through provider adapters, which are not ported |
| `xpst_auth_status` | probes platform credentials, which needs the network |
| `xpst_byo_app` | reads and decrypts the bring-your-own-app store, which is not ported |
| `xpst_preflight` | resolves destinations against live credentials and the content contract |
| `xpst_auth_start` | builds the per-platform auth plan from the auth flow, which is not ported |
| `xpst_disconnect` | removes stored credentials through Python services, which are not ported |
| `xpst_delete` | deletes posts on the platform, which needs provider adapters |
| `messenger_send` | sends through the Meta Graph API |
| `messenger_set_rules` | writes the Messenger rules through Python config services, which are not ported |
| `xpst_messenger_check_comments` | reads and replies on the Meta Graph API |
| `kb_add` | ingests into the knowledge store, which is not ported |
| `kb_organize` | writes the knowledge store, which is not ported |
| `xpst_setup_start` | writes the setup transaction file through Python services, which are not ported |
| `xpst_setup_status` | reads the setup transaction service, which is not ported |
| `xpst_setup_resume` | writes the setup transaction file through Python services, which are not ported |
| `xpst_setup_reset` | removes the setup transaction file through Python services, which are not ported |
| `xpst_stream` | reads the stream registry and can probe live channels |
| `xpst_chat` | opens live chat transports over the network |

That list is not a promise that they will be ported in that order. It is the
current truth, and the parity test fails if this document and the registry
disagree about the ported set.

## Consent tiering

Ported from `_guardrail_block()` in the Python server, message for message.

- Every tool carries a `read_only` classification. Mutating tools are exactly
  the Python `_MUTATING_TOOLS` set (`guard.rs`), asserted against that source by
  the parity test.
- `XPST_MCP_READONLY=1` refuses every mutating call outright.
- Without `XPST_MCP_ALLOW_MUTATIONS=1`, mutating calls are refused by default.
- `XPST_MCP_REQUIRE_CONFIRM=1` allows them only when the call carries
  `"confirm": true`.
- The guard runs **before** dispatch, so a refused call never reaches a handler.
  Pausing the schedule queue and the `xpst_setup_*` onboarding transaction are
  deliberately *not* gated — stopping posts is always safe and first-run setup
  happens before an operator opts into mutations, exactly as in Python.

## Tests

| File | What it proves |
|---|---|
| `tests/parity.rs` | Rust names equal the Python names, in order; the fixture is not stale; every implemented schema has `additionalProperties: false`; consent tiers match `_MUTATING_TOOLS`; every stub answers `not_implemented`; the ported set is this README's set. |
| `tests/guard.rs` | Default denial, read-only precedence, confirm-per-call, and the un-gated carve-outs. |
| `tests/tools.rs` | Each ported tool against a fixture store: masking, statistics, cooldowns, schedule ordering/pause/snapshots, provider catalog. |
| `tests/stdio_smoke.rs` | The real binary speaks MCP: initialize, tools/list (46 tools), a successful call, a consent refusal, and a `not_implemented` refusal. |

The parity test reads `../../src/xpst/mcp/server.py` directly — no Python
interpreter, no installed package, no network — so it runs in any `cargo test`.

Prove it can fail: rename a tool in `registry.rs`, or flip a tool's `Consent`,
or set a schema's `additionalProperties` to `true` in the fixture, and the
corresponding test goes red.

### Regenerating the fixtures

Only when the Python registry changes, and never by hand:

```bash
PYTHONPATH=src python scripts/capture_mcp_tool_registry.py
```

Re-running it must reproduce both fixtures byte-for-byte.

## Running

```bash
cargo run -p xpst-mcp          # MCP over stdio, XPST_CONFIG_DIR honoured
```

Diagnostics go to stderr; stdout carries MCP messages only. The HTTP API is out
of scope for this crate.

## Known divergences from Python (deliberate, documented)

- `xpst_config_show` reports `not_found` when there is no `config.yaml`. The
  Python tool renders its in-memory default config; those defaults are not
  ported, and inventing them would be a fake payload.
- `xpst_readiness`'s `python` row reports the Rust engine instead of a Python
  version (a Rust binary has no interpreter to version). The row keeps its id,
  label, severity and `ok`, so the summary and the blocking list are unaffected;
  the protocol fixture masks `message`/`details` for that one row, exactly as
  `local_surface.json` does.
- The Python server initializes its engine at startup, which creates
  `credentials/` and `backups/` before the first tool call; the Rust server has
  no engine. The protocol fixture seeds those two folders so the readiness
  `directories` row is the same document on both engines.
- `xpst_capabilities` serves the OFFLINE catalog: `state` is what the config and
  the credential store say, and every live-derived fact (`authenticated`,
  `session_valid`, `live_checked`, `probe_*`) is `null` rather than `false`.
  `xpst auth status` is the surface that probes.
- `xpst_transcript`'s filename-substring fallback sorts the directory listing
  before taking the first match. `os.listdir` order is filesystem order, so
  Python's "first match" is not reproducible; a single match is identical.
- `xpst_activity` renders a non-string `last_attempt` as its JSON text when
  sorting. Python compares the raw value and raises `TypeError` on a mixed-type
  ledger, which no shipped writer produces.
- The served schemas come from the captured fixture, so they track Python by
  regeneration rather than by parallel hand-editing.

## Dependencies

`rmcp` is the approved first-party SDK (`docs/technical/licence-policy.md`) and
is pinned to **3.11.0**. The pin is exact rather than a caret range because what
an MCP client sees — negotiated spec version, tool schemas, result shape — is
this project's primary agent surface, so an SDK upgrade should be a reviewed diff,
not something a lockfile refresh can do on its own.

3.11.0 is deliberate on two counts. It is the newest 3.x, and it is the first
line that carries both advisory fixes (1.4.0 for the Streamable HTTP server
transport, 3.9.0 for the HTTP clients), so the two ignores this crate used to
justify are gone. The negotiated protocol set is *not* left to the SDK default:
`server::SUPPORTED_PROTOCOL_VERSIONS` names the four `initialize`-handshake
generations explicitly, and `tests/stdio_smoke.rs` pins the matrix from the
client's side.

`jiff` renders `scheduled_time_local` in the entry's own zone, as Python does,
and reads rate-limit cooldown instants. It is Unlicense/MIT and reaches the IANA
database through the platform's zoneinfo files, with a bundled copy on targets
that have none (Windows) — which matters because the crate it replaced,
`chrono-tz`, carried the zone *data* and was flagged unmaintained, so its rules
would have frozen at the last release.

### Advisories

This crate no longer carries an ignored advisory. rmcp 0.11.0 had two, both for
the **Streamable HTTP** transports (RUSTSEC-2026-0189, DNS rebinding in the HTTP
server transport; RUSTSEC-2026-0462, unbounded response buffering in the HTTP
clients), and both are fixed in 3.11.0. This crate still enables only `server` +
`transport-io` and speaks stdio — rmcp's defaults enable neither HTTP transport —
so no HTTP listener or HTTP client is compiled in either way, and
`crates/xpst-core/tests/advisory_policy.rs` fails the build if that ever changes.

The advisory policy lives in `deny.toml`, argued in full in
`docs/technical/security-advisories.md`; `.cargo/audit.toml` carries the same
decisions for `cargo audit`. `crates/xpst-core/tests/advisory_policy.rs` fails the
build if this crate ever enables one of the HTTP transports, so the stdio-only
argument cannot go stale silently.
