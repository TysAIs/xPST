# Dependency advisories: what is fixed, what is ignored, and why

The `Licence policy` CI job runs `cargo deny check licenses bans sources
advisories` over **two** lockfiles — the engine workspace (`Cargo.lock`) and the
Tauri shell (`src-tauri/Cargo.lock`). Licences, bans and sources describe policy;
*advisories* describe the world outside the repository, and the world changes on
its own schedule. This note is the long form of every entry in `deny.toml`'s
`[advisories] ignore` list.

The rule that makes the gate mean something: **an ignore is a decision, not a
silence.** Every ignored advisory below names the affected crate, why the advisory
does not apply to what this repository builds or why it cannot be cleared, and the
date it was reviewed. `crates/xpst-core/tests/advisory_policy.rs` fails the build
if `deny.toml` and this document stop agreeing, if an ignore loses its reason, or
if `xpst-mcp` enables one of the rmcp transports the ignores depend on.

## Running it

```bash
cargo install cargo-deny --locked      # or: taiki-e/install-action@cargo-deny
cargo deny check advisories                                    # engine workspace
cargo deny --manifest-path src-tauri/Cargo.toml check advisories   # Tauri shell
cargo deny check licenses bans sources advisories               # what CI runs
```

`deny.toml` sets `[graph] all-features = true`, so the advisory graph prices
every feature combination the workspace *could* build, not just the ones it
does. That is deliberately stricter than the shipped binary and is why an ignore
must argue about the shipped feature set rather than the resolved one.

One consequence to expect and not "fix": the policy file is shared by both
lockfiles, so an ignore that names a shell-only crate reports `no crate matched
advisory criteria` in the engine graph, and vice versa. Those messages are
warnings, the check still exits 0, and they are correct — the ignore simply does
not apply to that graph. If you ever see the reverse (a real advisory with no
ignore), that is a finding, not noise.

## State after the 2026-11-18 pass

Measured, not remembered:

| Lockfile | Before | After |
|----------|--------|-------|
| Engine workspace | 4 vulnerabilities + 3 unmaintained | 0 vulnerabilities + 1 unmaintained |
| Tauri shell | 12 vulnerabilities + 9 unmaintained + 1 yanked crate | 0 vulnerabilities, 4 unmaintained, no yanked crate |

Both now exit 0 on `cargo deny check licenses bans sources advisories`.

The last two engine vulnerabilities were the rmcp pair; they cleared when the
MCP SDK moved 0.11.0 → 3.11.0 (see the rmcp row below for what a client sees).

### Cleared by replacing the crate

An unmaintained notice has no fixed release to move to, so the only way it
leaves this table is by the crate leaving the graph.

| Advisory | Crate | What replaced it |
|----------|-------|------------------|
| RUSTSEC-2026-0555 | `chrono-tz` 0.10.4 | Gone. `xpst-mcp` was its only caller (one IANA-zone lookup for `scheduled_time_local`); it now uses `jiff`, which reads the platform's zoneinfo database and carries a bundled copy for targets without one (Windows). |
| RUSTSEC-2024-0449 | `unsafe-libyaml` 0.2.11 | Gone. It was reachable only through `serde_yaml` 0.9.34+deprecated in `xpst-config` and `xpst-verify`. The maintained forks (`serde_yaml_ng`, `serde_norway`) keep the same c2rust translation under a new name, so the wrapper was replaced instead: the parser is now `serde-saphyr` (safe Rust, no C dependency, `#![forbid(unsafe_code)]`) behind this repository's own `Value`/`Mapping` model in `crates/xpst-config/src/yaml.rs`. |

### Cleared by upgrading

| Advisory | Crate | Fix | What moved |
|----------|-------|-----|------------|
| RUSTSEC-2026-0009 | `time` 0.3.45 | stack exhaustion parsing RFC 2822 input; fixed in 0.3.47 | `time` 0.3.45 → 0.3.55. The ceiling pin `>=0.3.40, <0.3.46` in `crates/xpst-provider-youtube/Cargo.toml` was an MSRV guard for a 1.85 floor that no longer exists; it was blocking the patch. |
| RUSTSEC-2026-0459 | `libsqlite3-sys` 0.38.2 | bundled SQLite before 3.54.0 can return uninitialised page-cache memory for a crafted database + `-wal` pair; fixed in `libsqlite3-sys` 0.39.0 | `rusqlite` 0.40.2 → 0.41.1, `libsqlite3-sys` 0.38.2 → 0.39.1 (bundled SQLite 3.54.1). |
| RUSTSEC-2026-0285, -0346, -0348, -0349 | `rustls` 0.23.43 | TLS 1.3 handshake boundary handling, `Debug` impls exposing session secrets, Brotli decompression allocation, key-share prediction | `rustls` → 0.23.46 |
| RUSTSEC-2026-0382 | `rustls-webpki` 0.103.15 | distrusted emSign intermediates accepted | → 0.103.17 |
| RUSTSEC-2026-0383 | `webpki-root-certs` 1.0.9 | bundled trust anchors include `emSign ECC Root CA - G3` | → 1.0.12 |
| RUSTSEC-2026-0367, -0368, -0369, -0429, -0545, -0548 | `zip` 4.6.1 | SUID/SGID preservation on extract, symlink targets outside the destination, allocation abort on a declared size, unverified AES tags, stream/named-header mismatch | `zip` → 9.7.0, which requires `tauri-plugin-updater` 2.14.1, which requires `tauri` 2.12 |
| RUSTSEC-2025-0075, -0080, -0081, -0098, -0100 | `unic-*` 0.9 | whole `rust-unic` family unmaintained | Gone from the graph: `tauri-utils` 2.10.4 uses `urlpattern` 0.6, which no longer needs `rust-unic` |
| (yanked) | `async-executor` 1.14.0 | yanked release | → 1.14.1 |
| RUSTSEC-2026-0189 | `rmcp` 0.11.0 | DNS rebinding in the Streamable HTTP **server** transport; fixed in 1.4.0 | `rmcp` 0.11.0 → 3.11.0 |
| RUSTSEC-2026-0462 | `rmcp` 0.11.0 | unbounded response buffering in the Streamable HTTP **clients**; fixed in 3.9.0 | same move |

The shell movement is one change: `tauri` 2.11.5 → 2.12.7 with its first-party
plugins (`tauri-plugin-updater` 2.14.1, `tauri-plugin-single-instance` 2.5.5,
`tauri-plugin-dialog`, `-shell`, `-deep-link`). Its 11 unit tests pass on the new
stack, and `rust-version` moved 1.77 → 1.90 because the new plugins declare 1.90.

### Ignored, with reasons

Five entries remain, and none of them is a vulnerability: every one is an
unmaintained-crate notice, and none has a release that would remove it today.
Four are shell-only (the `zbus` 5 chain and a GTK3 proc-macro); one is the
engine's single surviving ignore.

| Advisory | Crate | Kind | Why it cannot bite | Removal trigger |
|----------|-------|------|--------------------|-----------------|
| RUSTSEC-2026-0543 | `chrono` 0.4.45 | unmaintained (all versions) | Unmaintained ≠ vulnerable; there is no fixed release. This workspace no longer calls it — the three direct uses (rendering `scheduled_time_local`, parsing rate-limit cooldown timestamps, credential expiry checks) were replaced with `jiff` — so what remains is a third-party edge: `atrium-api` (Bluesky record timestamps), `schemars` and `rmcp`. No `chrono` type appears in an API of ours, and the advisory DB's concern is maintenance rather than a reachable defect. | `atrium-api`, `rmcp` or `schemars` dropping it |
| RUSTSEC-2024-0370 | `proc-macro-error` 1.0.4 | unmaintained | Proc-macro dependency of `glib-macros`/`gtk3-macros` (Tauri's GTK3 stack): it executes at **compile time** and is not linked into the shipped binary. | gtk-rs beyond 0.18 upstream in Tauri |
| RUSTSEC-2026-0370 | `zvariant` 5.15.0 | unmaintained | Merged into `zbus` 6, but `tauri-plugin-single-instance` 2.5.5 — the newest 2.x — pins `zbus ^5.9`. No vulnerable code, only a maintenance statement. | `tauri-plugin-single-instance` 3.x |
| RUSTSEC-2026-0371 | `zbus_names` 4.3.4 | unmaintained | Same `zbus` 5 chain, same upstream pin. | Same |
| RUSTSEC-2026-0372 | `zvariant_derive` 5.15.0 | unmaintained | Same chain; a derive macro, compile time only. | Same |

Both halves of that follow-up are done. The MCP SDK move below cleared the two
rmcp advisories, and replacing `chrono`/`chrono-tz` and `serde_yaml` cleared the
other two — the ignore list therefore carries no vulnerability, and the engine's
one survivor is `chrono`, reached only by third-party code.

## The MCP SDK move (rmcp 0.11.0 → 3.11.0)

The two rmcp advisories were the only vulnerabilities that list ever carried. They
were cleared by moving the SDK rather than by re-arguing the feature set: the
server-transport fix is 1.4.0 and the client-transport fix is 3.9.0, so 3.11.0 is
the current line that carries both. `xpst-mcp` still enables only `server` +
`transport-io` — nothing in this move turns an HTTP transport on.

What a client sees is asserted, not assumed.
`crates/xpst-mcp/tests/stdio_smoke.rs` drives the real binary and pins the
negotiation matrix:

| Client asks for | Server negotiates | Why |
|---|---|---|
| 2024-11-05 | 2024-11-05 | echoed |
| 2025-03-26 | 2025-03-26 | echoed |
| 2025-06-18 | 2025-06-18 | echoed — 0.11.0 downgraded these clients to 2025-03-26 |
| 2025-11-25 | 2025-11-25 | echoed |
| 2026-07-28 | 2025-11-25 | that revision has no `initialize` handshake, so a client that opens with the handshake is served the newest handshake revision instead |

A client that speaks *only* the 2026-07-28 generation never sends `initialize` —
it puts the version in each request's `_meta` — and that request is refused with
`-32022 Unsupported protocol version` naming the four revisions above. That
refusal is deliberate rather than incidental: this server would have to serve a
lifecycle nothing here tests, and 0.11.0 did worse than refuse — it answered
nothing at all, leaving the client to time out.

The declared set is `xpst_mcp::SUPPORTED_PROTOCOL_VERSIONS`: the four
`initialize`-handshake generations, stated explicitly so a later SDK bump cannot
silently widen what this server claims to speak. Apart from the negotiated
`protocolVersion` — the one field the table above moves — nothing else on the
wire changed: the 46 `tools/list` schemas, the consent refusals and the tool
result shape (`isError` + `content[0].text`) are byte-identical under every
negotiated version, and a capture diffed path by path across all five requested
versions differed in exactly three paths, all of them `protocolVersion`.

## Why the toolchain floor moved

`rust-version` is a promise about what builds this tree, and the previous value
was already false before any of this work:

```
$ cargo +1.85.0 check --workspace --all-targets
error: rustc 1.85.0 is not supported by the following packages:
  encoding_rs@0.8.44 requires rustc 1.88
  uuid@1.29.3 requires rustc 1.89.0
```

The engine workspace now declares `rust-version = "1.89"`, which is both the
lowest floor the committed graph actually builds on and the lowest floor that
admits the patched `time` and `rusqlite` releases. It is verified, not asserted:

```bash
cargo +1.89.0 check --workspace --all-targets
```

The caps that pinned `idna_adapter` and the `icu_*` family were MSRV guards for
the 1.85 floor; with the floor gone they only guaranteed stale Unicode data, so
they were removed rather than re-pointed.

`cargo-deny` itself is not a dependency of this project: CI installs it with
`taiki-e/install-action@cargo-deny` (currently 0.21.0). Local runs need
`cargo install cargo-deny --locked`.

## Reviewing this again

Re-run both commands above when: the Rust engine job adds or drops a dependency
family, a Tauri plugin moves a major version, or six months pass. An ignore whose
`Removal trigger` has fired is a bug, not a stale comment — clear it in the same
PR that moves the dependency.
