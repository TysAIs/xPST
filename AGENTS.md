# xPST — Cross-Posting Suite

**Enterprise-grade Python CLI/desktop app for automated video cross-posting to YouTube, X, and Instagram, with TikTok as a video source (TikTok publishing is not available yet — see docs/INSTALL.md capability truth table).**

## Quick Start

```bash
cd ~/XPST
source .venv/bin/activate
python -m xpst --help          # CLI with 45 commands
python -m xpst run             # Run the cross-posting engine
python -m xpst dashboard       # Start FastAPI dashboard (port 8080)
python -m xpst app             # Launch the installed desktop app (Tauri shell)
python -m pytest tests/        # full suite (1555 passed, 2 skipped)
```

## Architecture

| Layer | Location | Responsibility |
|-------|----------|----------------|
| **CLI** | `src/xpst/cli.py` | 45 commands, `--json`, `--dry-run`, structured exit codes |
| **Engine** | `src/xpst/engine.py` | `CrossPostEngine` orchestrator (check_and_post, backfill, delete_post, health) |
| **Platforms** | `src/xpst/platforms/` | YouTube, X, Instagram uploaders (auth via SessionManager) |
| **Sources** | `src/xpst/sources/` | TikTok, Instagram Reels, Local files |
| **State** | `src/xpst/state_store.py` + `state_manager.py` | Atomic I/O + business logic (thread-safe) |
| **Config** | `src/xpst/config.py` | Pydantic settings, bcrypt dashboard auth, auto-migration v1→v4 |
| **Desktop** | `src-tauri/` + `ui/` | Tauri 2 shell over the Svelte dashboard UI, with the Python engine as a sidecar; `xpst app` launches it |
| **Dashboard** | `src/xpst/dashboard/server.py` | FastAPI + WebSocket, bcrypt auth |
| **MCP** | `src/xpst/mcp/server.py` | 40 tools (post, health, config, state, platforms, scheduling incl. cancel, targeted failure retry, analytics, KB, captions, ideas, bio, transcripts, search, Messenger DM + comment auto-reply) |

## Key Principles

- **FREE + OPEN SOURCE** — Zero personal data in distributable tools
- **Enterprise-grade quality** — 1555 passed, 2 skipped, thread-safe, encrypted credentials, bcrypt passwords
- **Agent-friendly CLI** — Auto-JSON on non-TTY, `--quiet`, `--dry-run`, exit codes 0/1/2/3/4/10
- **No hardcoded secrets** — All via `~/.xpst/` or env vars
- **Apple-like UI standard** — Light/dark mode, Inter font, accessible landmarks/labels in the dashboard UI (`ui/`)

## Non-Negotiables

- Never push to production directly — PRs only
- Never write customer data from untrusted web sources
- All external calls async (`run_in_executor` for blocking Google APIs, `asyncio.create_subprocess_exec` for yt-dlp)
- Threading.Lock for StateManager (supports sync tests)
- SessionManager = single source of truth for ALL platform/auth
- Config auto-migrates on load (v1→v4)

## Common Commands

```bash
# Tests
python -m pytest tests/test_state.py tests/test_config.py tests/test_monitor.py -v
python -m pytest tests/test_hardening.py -v

# Build the desktop app (Tauri shell + Python engine sidecar)
scripts/build-engine.sh                  # engine sidecar
cd src-tauri && cargo tauri build        # shell + installer for this OS

# Code quality
ruff check src/
mypy src/xpst/
```

## Environment

- Python 3.11+ (venv at `~/XPST/.venv/`)
- FastAPI, authlib, httpx, bcrypt, cryptography, pydantic-settings
- FFmpeg on PATH (or set `XPST_FFMPEG_PATH`)

## Memory Notes (persistent)

- Config dir: `~/.xpst/` (state.json, credentials.enc, translations/)
- Dashboard password hash in config (bcrypt)
- FFmpeg path auto-detected or configurable

---

**When working on xPST: Load this context, work in `~/XPST/`, use `.venv` Python.**