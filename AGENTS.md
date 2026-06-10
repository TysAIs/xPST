# xPST — Cross-Posting Suite

**Enterprise-grade Python CLI/desktop app for automated video cross-posting to YouTube, X, Instagram, TikTok.**

## Quick Start

```bash
cd ~/XPST
source .venv/bin/activate
python -m xpst --help          # CLI with 22 commands
python -m xpst run             # Run the cross-posting engine
python -m xpst dashboard       # Start FastAPI dashboard (port 8080)
python -m xpst desktop         # Launch PySide6/QML desktop app
python -m pytest tests/        # 793 tests (core 327 pass fast)
```

## Architecture

| Layer | Location | Responsibility |
|-------|----------|----------------|
| **CLI** | `src/xpst/cli.py` | 22 commands, `--json`, `--dry-run`, structured exit codes |
| **Engine v2** | `src/xpst/engine_v2.py` | Orchestrates use-cases via DI |
| **Use-Cases** | `src/xpst/usecases/` | `FetchNewVideos`, `CrossPostVideo`, `ManualPost`, `Backfill`, `HealthCheck`, `DeletePost` |
| **Platforms** | `src/xpst/platforms/` | YouTube, X, Instagram uploaders (auth via SessionManager) |
| **Sources** | `src/xpst/sources/` | TikTok, Instagram Reels, Local files |
| **State** | `src/xpst/state_store.py` + `state_manager.py` | Atomic I/O + business logic (thread-safe) |
| **Config** | `src/xpst/config.py` | Pydantic settings, bcrypt dashboard auth, auto-migration v1→v4 |
| **Desktop** | `src/xpst/desktop_app/` | PySide6/QML (10 pages), splash, i18n, plugins |
| **Dashboard** | `src/xpst/dashboard/server.py` | FastAPI + WebSocket, bcrypt auth |
| **MCP** | `src/xpst/mcp/server.py` | 8 tools (fetch, post, health, config, state, platforms) |

## Key Principles

- **FREE + OPEN SOURCE** — Zero personal data in distributable tools
- **Enterprise-grade quality** — 793 tests, thread-safe, encrypted credentials, bcrypt passwords
- **Agent-friendly CLI** — Auto-JSON on non-TTY, `--quiet`, `--dry-run`, exit codes 0/1/2/3/4/10
- **No hardcoded secrets** — All via `~/.xpst/` or env vars
- **Apple-like UI standard** — Light/dark mode, Inter font, accessibility (Accessible.role/name)

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

# Build
./build.sh macos          # PyInstaller .app bundle

# Code quality
ruff check src/
mypy src/xpst/
```

## Environment

- Python 3.11+ (venv at `~/XPST/.venv/`)
- PySide6, FastAPI, authlib, httpx, bcrypt, cryptography, pydantic-settings
- FFmpeg on PATH (or set `XPST_FFMPEG_PATH`)

## Memory Notes (persistent)

- Config dir: `~/.xpst/` (state.json, credentials.enc, translations/)
- Dashboard password hash in config (bcrypt)
- FFmpeg path auto-detected or configurable

---

**When working on xPST: Load this context, work in `~/XPST/`, use `.venv` Python.**