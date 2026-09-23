# AGENTS.md — working on xPST

xPST is a cross-posting suite: it takes short-form video from a source (TikTok,
Instagram Reels, YouTube, local files) and publishes it to YouTube Shorts, X/Twitter,
Instagram Reels and Threads, tracking state and analytics locally. MIT OR Apache-2.0.
TikTok publishing is not available yet — see the capability truth table in
`docs/INSTALL.md`.

This file is the map an agent needs **before** touching the repo. Read it first. Other
docs are written for humans and some of them still lag the code.

## Rules that are not negotiable

1. **PRs only. Never push to `main`.** Branch, push the branch, open a PR. Pushing a
   commit-ish to an explicit ref is the only safe push form:
   `git push origin <sha>:refs/heads/<branch>`; then confirm with
   `git rev-parse origin/<branch>` (a silent no-op push prints nothing and looks like
   success). Never force-update `main`.
2. **Never mutate the shared `~/xPST` checkout.** Do not `git checkout`/`switch`,
   `reset`, `restore`, `clean`, `rebase`, and do not build in it — multiple agents share
   that tree and a branch switch under another agent has already destroyed shipped work.
   Work in a throwaway clone instead (see *Isolated work* below). The long-form version
   of this rule is `AGENTS-CHECKOUT-RULES.md`.
3. **Never claim a result you did not observe.** No fabricated test output, no "should
   work", no "pushed OK" without a verified remote ref. If something needs a human, say
   which human and which step.
4. **Zero personal data in the repo or in build artifacts.** No credentials, tokens,
   cookies, real account IDs or personal media anywhere in the tree, in test fixtures or
   in committed build output. Secrets live only under `~/.xpst/` or in env vars.

## Architecture (current)

The product is a **Python engine** with a **thin Rust/Tauri shell** around it. The engine
is the whole application; the shell is a wrapper.

| Layer | Location | Responsibility |
|-------|----------|----------------|
| **Engine** | `src/xpst/engine.py` | `CrossPostEngine` — the single orchestrator that every surface calls (check-and-post, backfill, delete, health). |
| **Platforms** | `src/xpst/platforms/` | Destination uploaders (YouTube, X, Instagram, TikTok, Threads) + Messenger for messaging; auth through `SessionManager`. |
| **Sources** | `src/xpst/sources/` | TikTok, Instagram Reels, YouTube, local files. |
| **State** | `src/xpst/state_store.py`, `state_manager.py` | Atomic write-then-rename persistence, thread-safe business logic, crash recovery. |
| **Config** | `src/xpst/config.py`, `config_migration.py` | Pydantic settings, schema auto-migration on load. |
| **Credentials** | `src/xpst/utils/credentials.py` | Encrypted-at-rest store (Fernet file store by default; OS keyring is opt-in). |
| **MCP** | `src/xpst/mcp/server.py` | 40 tools (stdio, typed schemas from `tools/list`, typed errors, fail-closed mutation guards; post, health, config, state, platforms, scheduling incl. cancel, targeted failure retry, analytics, KB, captions, ideas, bio, transcripts, search, Messenger DM + comment auto-reply). **Primary agent surface.** |
| **CLI** | `src/xpst/cli.py` | **Scriptable fallback.** `--json` (automatic on non-TTY), `--dry-run`, structured exit codes. |
| **HTTP API + web UI** | `src/xpst/dashboard/server.py`, `ui/` | FastAPI + WebSocket serving the Svelte/Vite UI. **Internal to the app** — not a supported third-party API surface. |
| **Desktop shell** | `src-tauri/src/` | Rust/Tauri shell (~850 LOC). Picks a free port, spawns the engine sidecar, waits for health, navigates the webview to it, forwards `xpst://` deep links to the engine's OAuth callback, kills the sidecar on exit, bounded crash-respawn. Everything product-facing lives in the engine, not here. |
| **Plugin system** | `src/xpst/plugins/` | Loads platform plugins from `~/.xpst/plugins/`; each plugin file defines `register()` returning an uploader class, a source class, or both (ABCs in `src/xpst/platforms/base.py`, `src/xpst/sources/base.py`). |

Two rules follow from that table:

- **Put behaviour in the engine.** A feature implemented in the Rust shell or in
  `ui/src/` is invisible to MCP and CLI clients. Surfaces are thin views over the engine.
- **MCP is the agent contract.** If you add an engine capability that an agent should be
  able to invoke, add the MCP tool for it; the CLI is the fallback for what MCP does not
  cover (see *Agent-surface behaviour you must know*).

### Repo map

```
src/xpst/      Python engine (the product)
src/xpst/mcp/  MCP server — primary agent surface
src/xpst/dashboard/  FastAPI + WebSocket server behind the UI
src-tauri/     Rust/Tauri desktop shell (spawns the engine sidecar)
ui/            Svelte + Vite + Tailwind web UI (built into the shell)
tests/         pytest suite (engine, CLI, MCP, HTTP API)
ui/tests/      node --test suite for the web UI
scripts/       build, signing, release, verification helpers
docs/          human-facing documentation (see the drift warning below)
```

## Isolated work (the checkout rule, concretely)

```bash
git clone https://github.com/TysAIs/xPST.git /tmp/xpst-work-<task>
cd /tmp/xpst-work-<task>
git fetch https://github.com/TysAIs/xPST.git main:refs/remotes/origin/main
git checkout -b <branch> origin/main        # branch from fresh GitHub main, not a local lag
export PYTHONPATH="$PWD/src"                # see the trap below
```

**The trap:** a developer venv here often has an *editable* xPST install whose path
config points at `~/xPST/src`. Without `PYTHONPATH="$PWD/src"` your tests and CLI runs
import the **shared tree**, so you can get green results for code you did not write.
Confirm which tree you are testing before you believe any result:

```bash
python -c "import xpst; print(xpst.__file__)"   # must be inside your clone
```

Push and open the PR from the clone; delete the clone once merged.

Builds that produce a distributed artifact are a coordinator decision from a pinned
commit in a dedicated worktree, never from a shared checkout.

## Commands that exist today

Set up (matches `CONTRIBUTING.md`):

```bash
python -m venv .venv && source .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -e ".[dev,mcp]"
```

The Click group in `src/xpst/cli.py` exposes 46 top-level commands (69 counting
subcommands); these are the ones an agent needs:

```bash
python -m xpst --help              # list all commands
python -m xpst status              # health status
python -m xpst doctor              # diagnose auth health, quotas, environment
python -m xpst readiness           # first-run readiness and next actions
python -m xpst auth status --json  # per-platform auth state, machine-readable
python -m xpst health              # connectivity check, no uploads
python -m xpst run --dry-run       # full pipeline without publishing
python -m xpst mcp                 # start the MCP server on stdio
python -m xpst ui                  # run the web UI + its API server locally
```

Quality gates (run these before you push):

```bash
python -m pytest tests/ -q         # full suite
ruff check src/                    # lint
mypy src/xpst/                     # types
cd ui && npm ci && npm test        # web UI tests
```

Do not write a test count into this file. Counts drift and a stale count is how the last
version of this map went wrong; run the suite and report what it says. The counts this
file does quote (MCP tools, CLI commands) are machine-checked: the MCP registry by
`tests/test_mcp_docs_registry_parity.py`, both by `tests/test_agents_docs_drift.py`.

## Agent-surface behaviour you must know

- **MCP mutation guards are fail-closed** and read from the environment:
  `XPST_MCP_READONLY`, `XPST_MCP_REQUIRE_CONFIRM`, `XPST_MCP_ALLOW_MUTATIONS`. An
  unwatched agent gets a refusal, not a surprise post. Do not weaken these defaults.
- **`xpst_delete` (MCP) and `xpst delete` (CLI) are not the same operation.** The MCP tool
  removes the local record; the CLI command also deletes on the platform. If an agent asks
  to "delete a post", use the CLI path deliberately and say what it does.
- **No OpenAI-compatible endpoint, and none should be added.** Agents speak MCP; the HTTP
  API exists for the UI. `/openapi.json` is there if you need to read schemas.
- **MCP now covers schedule-cancel and targeted retry** (`xpst_schedule_cancel`,
  `xpst_failures_retry`); the CLI equivalents (`python -m xpst schedule`,
  `python -m xpst failures`) remain for scripting. Prefer the MCP tool over teaching
  agents to shell out.

## Capability truth

Do not describe a platform capability this code cannot perform. Threads, TikTok
destination, Messenger and per-modality support all have real limits; the maintained
table is `docs/INSTALL.md#capability-truth-table`. When you change what a platform can
actually do, update that table in the same PR.

## State and storage

- Everything runtime lives under `~/.xpst/` (state, credentials, cache, translations).
  Nothing there is ever committed.
- Credentials are encrypted at rest; the Fernet file store is the default and the OS
  keyring is opt-in, so never assume a keychain is present.
- Config is validated by Pydantic and auto-migrates on load. If you touch the schema, add
  the migration and its test.

## Documentation drift

`AGENTS.md` is drift-tested: `tests/test_agents_docs_drift.py` fails if this file mentions
the deleted UI stack, quotes old counts, or names a CLI command that no longer exists.
The rest of `docs/` is not yet covered — if you find a stale claim in it, fix it in the
PR that made it stale, or open an issue naming the file and the claim.
