# Proposal: all-Rust engine rebuild

**Status**: proposal — follows [DIRECTION.md](DIRECTION.md) D-3/D-4, awaiting
maintainer sign-off on the open questions (see the end).
**Date**: 2026-11-03
**Baseline**: `origin/main` @ `5bdc39c` (engine installed at that commit,
`~/.venvs/xpst`).

This is a plan, not a rewrite in progress. Nothing in it changes behavior until
a phase lands with tests.

---

## 1. Why this is a rewrite and not a port

The engine is 146 Python files / 69,302 lines under `src/`, plus 242 test files /
66,420 lines. What it ships is one PyInstaller sidecar of ~95 MB inside a ~192 MB
app, and what it costs is a Python ≥ 3.10 install, a venv, 20 runtime
dependencies plus three extras, and 2.4 s of cold boot on the reference host.

The behaviors that matter are already specified by tests and by the truth
tables — capability states, post-gate limits, cooldown ownership, refusal over
truncation, readiness honesty. Those are the *contract*. A rewrite that carries
the contract and drops the interpreter is the goal; a syntax-level port that
carries the architecture of the workarounds is not worth doing.

| Surface | Today | Rust target |
|---|---|---|
| CLI | `click`, 35 top-level commands plus groups, `cli.py` 7,029 lines | `clap` derive, same command names/flags, `--help` parity list checked in a test |
| Dashboard API | FastAPI + uvicorn on localhost | `axum` + `tower-http`, same routes and JSON shapes |
| MCP server | 41 tools over the Python MCP SDK | `rmcp` (official Rust MCP SDK), same tool names/schemas |
| Desktop | Tauri 2 shell spawning a Python sidecar (1,070 lines Rust) | Tauri 2 shell with the engine *in-process* or as one child binary; UI unchanged |
| Engine | `asyncio` services + JSON state | `tokio` tasks + SQLite/JSON store, atomic writes preserved |

## 2. Crate map

A single Cargo workspace, one published binary, no dynamic plugins:

```
crates/
  xpst-cli          # clap surface; the only binary
  xpst-core         # engine orchestration: pipeline, retry ownership, cooldowns
  xpst-config       # config load/migrate (v1→v4 semantics), validation
  xpst-state        # atomic state store, DLQ, dedupe, schema migration
  xpst-auth         # OAuth (PKCE/device), cookie sessions, token refresh, keyring
  xpst-provider     # trait + registry; capability truth table as data
    + one crate per provider: youtube, x, instagram, tiktok, threads, meta
  xpst-source       # local files, yt-dlp subprocess adapter
  xpst-media        # ffmpeg subprocess adapter, probe/normalize/re-encode
  xpst-store        # analytics, snapshots, knowledge base (SQLite)
  xpst-api          # axum dashboard, serves the existing ui/dist
  xpst-mcp          # rmcp tool surface
  xpst-app          # Tauri shell (reuses ui/, keeps updater/deep-link plugins)
xtask               # dev/release automation (fixture capture, parity diff)
```

Rules that keep it "lightweight and easy" in D-4 terms:

- **One runtime.** `tokio`, `reqwest` with `rustls` (no OpenSSL), `serde`,
  `rusqlite` (bundled), `clap`, `axum`, `rmcp`. No plugin loader, no scripting
  layer.
- **No unofficial client libraries.** `twikit` and `instagrapi` are replaced by
  direct HTTP against documented endpoints. This is a feature: the unofficial
  stack is what pulled in `PIL` + `lxml` (+24.6 MB, measured in
  `bundle-size.yml`) and it is also the Instagram suspension risk.
- **Credential format is preserved on read.** Existing `Fernet + scrypt`
  credential files must keep working (`fernet` crate reads them); new writes use
  `argon2id` + `chacha20poly1305` with a version byte. Migration is lazy and
  reversible: the old file is only replaced after a successful re-read.
- **State format is preserved.** `version: 2` state reads forward; writes stay
  version 2 until a documented migration exists. A downgrade path is required
  for one release cycle (D-8 open question).
- **ffmpeg and yt-dlp are child processes**, discovered on `PATH` or bundled
  beside the binary, with versions pinned in a manifest the app verifies. Both
  are optional per feature: no yt-dlp means "no remote sources, local files
  still work"; no ffmpeg means "passthrough only, named as such in readiness".

## 3. Port map, sized by actual code

| Area | Python today | Rust plan | Notes |
|---|---|---|---|
| `cli.py` (7,029) | click | `xpst-cli` | Command names and exit codes are the contract; `docs/TUTORIAL_CLI.md#exit-codes-reference` is the spec |
| `connect.py` (2,118), `auth_flow.py` (968), `token_state.py` | OAuth + cookie flows | `xpst-auth` | PKCE, device code, cookie import, refresh, `appsecret_proof` |
| `engine.py` (1,797), `services/*` (3,754) | orchestration, retry, cooldown | `xpst-core` | Carry cooldown state machine and retry-ownership verbatim |
| `config.py` (1,650), `config_migration.py` | pydantic settings | `xpst-config` | Same YAML keys, same migrations |
| `state_manager.py` (977), `schedule_manager.py` (1,171) | atomic JSON | `xpst-state` | Write-then-rename + lock; schedule format unchanged |
| `platforms/*` (8,780) | 12 modules | `xpst-provider*` | YouTube, X first; Instagram Graph second; TikTok drafts, Threads, Meta messaging after |
| `sources/*` (2,791), `media/*` (1,988) | yt-dlp/ffmpeg wrappers | `xpst-source`, `xpst-media` | Subprocess adapters with pinned-version checks |
| `dashboard/*` (4,690) | FastAPI | `xpst-api` | Route-for-route; UI untouched |
| `mcp/*` (2,958) | Python MCP SDK | `xpst-mcp` | 41 tools, same schemas |
| `knowledge/*` (3,890) | extraction + store | `xpst-store` | Deterministic extraction first; LLM client last |
| `analytics*.py` (1,580+), `best_time.py` | metrics | `xpst-store` | SQLite snapshots |
| `utils/*` (8,312) | logging, retry, cooldown store | distributed | `tracing` replaces `structlog`; `metrics` replaces prometheus client |
| `src-tauri` (1,070) | shell | `xpst-app` | Already Rust; the sidecar spawn goes away |

Tests: the 242 Python test files are not ported line-for-line. The behaviors
they encode become (a) Rust unit/integration tests where the rule is mechanical,
and (b) golden parity fixtures where the rule is product truth.

## 4. Parity strategy — how a rewrite avoids drift

1. **Truth fixtures first.** Before a behavior is rewritten, capture the Python
   engine's observable output for a fixed input set: `provider_truth` table,
   post-gate verdicts, readiness payloads, health JSON, exit codes. Fixtures are
   generated by `xtask capture` from the reference engine and committed (with
   fictional accounts only).
2. **Differential harness.** `xtask parity <fixture>` runs the Rust build against
   the same fixture and diffs the JSON, with an allow-list for intentional
   changes. A phase is done only when its fixtures diff clean.
3. **Dual-run window.** For one release cycle, the release bundle can ship either
   engine (`--engine python|rust`), so a regression has an escape hatch. The
   flag is removed with the Python engine.
4. **No behavior invented during the port.** Anything the Python engine does
   oddly and a test pins gets ported as-is, with a `// parity: <why>` comment and
   a follow-up card if it should change.

## 5. Phases (each is a separate, reviewable card)

| Phase | Scope | Done when |
|---|---|---|
| P0 | Workspace scaffold, `xpst-cli` skeleton with the full command tree, config/state read paths, CI (fmt, clippy `-D warnings`, tests, `cargo audit`), checked-in deny list for the public-safety gate | `cargo test` green in CI; `xpst --help` covers the command list; tree scan clean; no Python code changed |
| P1 | Auth + YouTube and X health/read paths; truth table as data; parity fixtures for health/readiness | Parity diff clean against captured fixtures |
| P2 | Posting path for YouTube and X (refuse-don't-truncate, cooldown state machine, retry ownership), draft/dry-run first | Parity clean; one real post per platform behind the operator lane |
| P3 | Instagram Graph, TikTok drafts, Threads, Meta messaging | Parity clean per provider; capability table matches `docs/INSTALL.md` |
| P4 | `xpst-api` on axum + `xpst-mcp` on rmcp; UI talks to the Rust engine unchanged | Route/tool parity check; UI smoke test against the Rust engine |
| P5 | Packaging: single binary, `cargo-dist`, signed artifacts, boot/size budgets re-baselined, PyInstaller removed | Install ≤ 30 MB without media tools; cold boot ≤ 1.0 s; budgets updated with the measured number |
| P6 | Delete Python (`src/`, `pyproject.toml`, `build_engine.spec`, Python-only workflows, `uv.lock`) | Green CI on a repo with no Python |

Sequencing rationale: P0–P1 are cheap and reversible and prove the toolchain;
P2 is where behavior risk concentrates, so it comes with the strongest fixtures;
P4 is the surface-parity gate the desktop app needs; P5–P6 are the payoff and are
deliberately last.

## 6. Risks, and what each one costs

| Risk | Why it bites | Mitigation |
|---|---|---|
| `yt-dlp` is Python | "all-Rust" cannot be literal for TikTok/YouTube source extraction without reimplementing extractors | Keep it as a pinned external binary, optional, documented. Revisit only if it becomes the last thing forcing a Python install (it does not: it ships as a binary) |
| Unofficial X client behavior | `twikit` encodes quirks the API docs do not | Cookie-session path first, parity fixtures from the live-installed engine, and honest `badge_reason` text when a probe cannot verify |
| Instagram unofficial mode | `instagrapi` session mode has suspension risk and heavy deps | Port Graph API only; mark session mode "not recommended" or retire it (D-8) |
| OAuth edge cases | refresh, revocation, scope changes, clock skew | `xpst-auth` gets its own fixture suite plus token-state machine tests ported from `token_state.py` |
| State/config migration | users have live state v2 and config v4 | Read-forward only; write only after successful re-read; downgrade path for one release |
| Scope creep into the UI | the UI is 43 files of working React | UI is frozen for the rebuild; API shapes preserved instead |
| Rebuild stalls at 80% | classic rewrite failure | Each phase ships usable behavior; no phase depends on a later one to be useful |

## 7. Acceptance criteria for "the rebuild is done"

1. `git grep -l 'import ' src/` returns nothing — no Python source in the tree.
2. One binary; `xpst --version` and `xpst doctor` work on a machine with no
   Python installed (verified in CI on a bare container).
3. Every behavior in [DIRECTION.md](DIRECTION.md) D-7 has a Rust test naming it.
4. Install ≤ 30 MB without media tools; cold boot ≤ 1.0 s median on the
   reference host class, recorded in `perf/boot-budget.json`.
5. Dashboard, MCP (41 tools) and CLI surfaces answer with the same shapes as the
   `1.2.x` engine for the committed fixtures.
6. Public-safety tree+artifact scan is clean *with* the checked-in deny list
   active, and history scan has no unexpected categories.

## 8. Open questions for the maintainer

1. Keep the Python package installable for one release after the Rust release
   (escape hatch), or remove it immediately?
2. Does PyPI keep the `xpst` name through the rebuild, or does the Rust binary
   ship only via GitHub Releases (plus `cargo install`)?
3. Retire Instagram's unofficial session mode with the rebuild, or carry it?
4. Bundle ffmpeg (bigger download, zero setup) or fetch it on demand (smaller,
   one more failure mode)?
5. Phase order P2 (posting) before P4 (surfaces) — confirm, since the desktop
   app cannot use the Rust engine until P4.

Default answers assumed if nobody objects: keep the escape hatch for one release,
keep the PyPI name, retire unofficial Instagram, fetch media tools on demand, and
do P2 before P4.
