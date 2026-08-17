# Contributing to xPST

Thank you for helping make xPST better. The project goal is a free, open-source,
local-first cross-posting studio that creators can trust without subscribing to
a hosted service.

## Quick Start

Requirements:

- Python 3.10 or newer
- FFmpeg
- Git

```bash
git clone https://github.com/TysAIs/xPST.git
cd xPST
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # macOS/Linux
pip install -e ".[dev,mcp]"
xpst readiness --json
```

## Before Opening an Issue

- Search existing issues first.
- Never paste tokens, cookies, session files, OAuth secrets, or raw credential
  files.
- For setup problems, run `xpst diagnostics --json` and review the generated
  bundle before attaching it.
- Use the platform breakage, install failure, or provider request templates when
  they match your report.

Security vulnerabilities should follow [SECURITY.md](SECURITY.md), not public
issues.

## Pull Requests

1. Fork the repository.
2. Create a focused branch.
3. Keep unrelated refactors out of the PR.
4. Add or update tests for behavior changes.
5. Update docs when user-facing commands, configuration, provider behavior, or
   release steps change.
6. Run the relevant checks before opening the PR.

Recommended local checks:

```bash
pytest
ruff check src tests scripts/verify_qml_pages.py scripts/release_artifacts.py scripts/clean_install_smoke.py
mypy src/xpst scripts/release_artifacts.py scripts/clean_install_smoke.py
python scripts/verify_qml_pages.py
python -m build
python scripts/clean_install_smoke.py --dist dist --artifact both
python scripts/release_artifacts.py --dist dist --output-dir release --skip-checks
```

## Architecture

xPST is layered so each concern can be changed without rippling into the others.
Contributors should place new code in the matching layer:

| Layer | Location | Responsibility |
|-------|----------|----------------|
| CLI | `src/xpst/cli.py` | 34 commands; `--json`, `--dry-run`, structured exit codes |
| Engine v2 | `src/xpst/engine.py` (+ `services/` DI use-cases) | Orchestrates the fetch → encode → upload → track pipeline via dependency-injected `UploadService` and `SourceService`; the single entry point the CLI, desktop app, and MCP server share |
| Providers (destinations) | `src/xpst/platforms/` | YouTube, Instagram, X uploaders; auth via `SessionManager` (`utils/sessions.py`) |
| Provider metadata | `src/xpst/providers.py` | `ProviderManifest` / role / capability enums shared by sources and destinations |
| Sources | `src/xpst/sources/` | TikTok, Instagram Reels, YouTube, local files |
| Anti-bot pacing | `src/xpst/anti_bot.py` | Randomized delays, time-of-day awareness, rate limits, User-Agent rotation |
| State | `src/xpst/state_store.py` + `state_manager.py` | Atomic I/O (write-then-rename) + thread-safe business logic |
| Config | `src/xpst/config.py` | Pydantic settings, bcrypt dashboard auth, auto-migration v1→v4 |
| Credential storage | `src/xpst/utils/credentials.py` + `secure_io.py` | Encrypted file fallback (Fernet + scrypt, 0600); OS keychain opt-in via `XPST_USE_KEYRING=1` |
| Crash recovery | `src/xpst/crash_recovery.py` | Detects partially-completed uploads and queues them for retry |
| Scheduler | `src/xpst/schedule_manager.py` + `scheduler.py` | Scheduled posts, recurring rules, OS-level install |
| Analytics | `src/xpst/analytics.py` + `analytics_store.py` | Per-post engagement metrics with persistent SQLite history |
| Knowledge base | `src/xpst/knowledge/` | Transcription, cited nuggets, embeddings, vector search (Phase 3) |
| Desktop | `src/xpst/desktop_app/` | PySide6/QML (10 pages), splash, i18n, plugins |
| Dashboard | `src/xpst/dashboard/server.py` | FastAPI + WebSocket, bcrypt auth |
| MCP | `src/xpst/mcp/server.py` | 23 tools (post, health, config, state, platforms, scheduling, analytics, KB, captions, transcripts, search) |

### Phase 1–5 feature map

| Phase | Status | Features |
|-------|--------|----------|
| **1 — Core** | ✅ Complete | Full-fidelity cross-posting, orientation-aware encode/passthrough, circuit breakers, crash recovery, atomic state, encrypted credentials |
| **2 — Surfaces** | ✅ Complete | CLI `--json`/`--dry-run`, PySide6/QML desktop app, FastAPI dashboard, MCP server |
| **3 — Knowledge** | ✅ Complete | faster-whisper transcription, cited nuggets, fastembed + LanceDB vector search, knowledge areas & course outline |
| **4 — Hardening** | ✅ Complete | Unified analytics with history, anti-bot pacing, quota management, dead-letter queue, diagnostics bundles, state backup/restore, plugin system, i18n |
| **5 — Polish & Launch** | 🚧 In progress | Docs polish, security audit, release readiness, onboarding UX, final QA |

Key principles:

- **The engine is the orchestrator.** New flows live in `src/xpst/engine.py`
  (or a focused module it calls) rather than ad-hoc logic in the CLI or a
  platform uploader. The engine delegates to dependency-injected use-cases
  (`UploadService`, `SourceService` in `src/xpst/services/`) — keep platform-specific
  behavior out of the engine and out of the service layer.
- **Providers are adapters, not orchestrators.** Keep platform-specific
  behavior (auth, upload, delete, analytics) inside `src/xpst/platforms/` and
  `src/xpst/sources/`. The engine should stay provider-agnostic; shared
  capability metadata belongs in `src/xpst/providers.py`.
- **SessionManager is the single source of truth** for all platform auth
  state; do not read credentials directly in uploaders.
- **Config auto-migrates on load** (v1→v4). When adding a config field, add a
  migration step so existing users upgrade transparently.

## Provider Contributions

xPST is provider-agnostic. New sources and destinations should expose a
machine-readable provider manifest so CLI, desktop, MCP, diagnostics, and
release tooling can reason about capabilities consistently.

For a new destination provider:

1. Add a module under `src/xpst/platforms/`.
2. Inherit from the existing platform base class.
3. Implement upload, health, and any supported delete/analytics behavior.
4. Add a `ProviderManifest` with roles, capabilities, auth mode, docs URL, and
   risk notes.
5. Add tests for the provider contract and failure isolation.

For a new source provider:

1. Add a module under `src/xpst/sources/`.
2. Inherit from the existing source base class.
3. Implement listing, download, health, and capability metadata.
4. Add fake-provider or mocked tests for auth, rate limits, network failures,
   and malformed API responses.

Provider integrations must be honest about official API status and platform
Terms risk.

## Coding Guidelines

- Prefer existing project patterns over new abstractions.
- Keep user data local by default.
- Avoid logging captions, credentials, cookies, tokens, or raw local paths.
- Use structured JSON outputs for commands that may be consumed by agents or
  scripts.
- Keep platform-specific behavior inside provider/source adapters rather than
  the engine, desktop UI, updater, or state layer.

## Testing Expectations

Tests should cover:

- Success and failure paths.
- Provider failure isolation.
- JSON command output contracts.
- Config migration and corrupted-file behavior when relevant.
- State persistence, retries, and recovery for workflow changes.
- Redaction for diagnostics or logs that include user-controlled text.

Use fake providers where possible instead of relying on live platform accounts.

## Documentation

Update the relevant docs when behavior changes:

- `README.md` for user-facing commands and feature claims.
- `docs/AGENT_GUIDE.md` for JSON/automation workflows.
- `docs/MCP_TOOLS.md` for MCP tools and resources.
- `docs/TUTORIAL_CLI.md`, `docs/TUTORIAL_APP.md`, `docs/TUTORIAL_MCP.md` for
  step-by-step user guides (note: these reference screenshot placeholders in
  `docs/assets/` that must be captured before a public release).
- `docs/ARCHITECTURE.md` for structural and design changes.
- `docs/LAUNCH_CHECKLIST.md` and `docs/ENTERPRISE_READINESS.md` for release
  gates.
- Provider-specific docs for auth, rate limits, and platform caveats.

## Release Notes

User-visible changes should be reflected in `CHANGELOG.md`. Release artifacts
must include checksums, SBOM, notices, license information, and release notes.

## Community

- Issues: https://github.com/TysAIs/xPST/issues
- Discussions: https://github.com/TysAIs/xPST/discussions
- Pull requests: https://github.com/TysAIs/xPST/pulls

Be respectful, specific, and practical. Small focused improvements are very
welcome.
