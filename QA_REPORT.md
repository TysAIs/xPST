# xPST Inline QA Report — Enterprise Readiness Assessment

**Date:** 2026-06-09  
**Codebase:** ~/XPST/ — Python 3.11, 39 modules, ~15k lines, 793 tests  
**Status:** ⚠️ **NOT ENTERPRISE READY** — Critical issues block production use

---

## Executive Summary

xPST is a **feature-complete prototype** with excellent test coverage (793 tests, all passing) and rich functionality (22 CLI commands, MCP server, PySide6 desktop app, dashboard). However, it suffers from **architectural debt** that makes it fragile at scale:

| Category | Rating | Blockers |
|----------|--------|----------|
| **Architecture** | 🔴 CRITICAL | God object (engine.py), no use-case layer, 0% test coverage on engine |
| **Thread Safety** | 🔴 CRITICAL | StateManager mutations without locks |
| **Async/Sync** | 🔴 CRITICAL | XUploader.delete() creates nested event loop (deadlocks) |
| **Security** | 🔴 CRITICAL | Dashboard password in plaintext, fallback credentials unencrypted |
| **Dependencies** | 🔴 CRITICAL | `authlib`/`httpx` missing from pyproject.toml, `ffmpeg-python` abandoned |
| **CLI/Automation** | 🟠 HIGH | No `--json`, no meaningful exit codes, not agent-friendly |
| **Cross-Platform** | 🟡 MEDIUM | Hardcoded ffmpeg paths, Windows desktop needs extra deps |

**Verdict:** Fix 🔴 CRITICAL items before any production deployment. Current state: **"works on my machine"** only.

---

## 🔴 CRITICAL — Must Fix Before Release

### 1. XUploader.delete() Creates Nested Event Loop (DEADLOCK)
**File:** `src/xpst/platforms/x.py:197-226`
```python
def delete(self, post_id: str) -> bool:
    loop = asyncio.new_event_loop()  # ← CRITICAL BUG
    loop.run_until_complete(client.delete_tweet(post_id))
```
**Impact:** Called from async engine → **RuntimeError or deadlock**.  
**Fix:** Make `delete()` async, update `PlatformUploader` base class.

### 2. StateManager Not Thread-Safe
**File:** `src/xpst/state.py:449-552` — `mark_video_posted`, `mark_video_failed`, `update_platform_health` mutate `_state` dict **without holding `_save_lock`**.  
**Impact:** Concurrent access (CLI + scheduler + dashboard) → state corruption.  
**Fix:** Wrap all mutations in `with self._save_lock:`.

### 3. Credential/Session Loading Duplicated (244 Lines)
Each platform re-implements auth loading instead of using `SessionManager`:
- YouTube: 145 lines (`platforms/youtube.py:61-206`)
- Instagram: 63 lines (`platforms/instagram.py:55-118`)
- X: 36 lines (`platforms/x.py:46-82`)
**Fix:** Delete duplicate code, use `SessionManager.get_*_client()` exclusively.

### 4. Dashboard Password Stored in Plaintext
**File:** `src/xpst/config.py:228, 696` — `dashboard_password: str = ""` serialized to YAML.  
**Fix:** Hash on save (bcrypt/argon2), compare hashes on login. Or use CredentialStore.

### 5. Fallback Credential Storage Unencrypted
**File:** `src/xpst/utils/credentials.py:87-89` — Plaintext JSON fallback contradicts docstring.  
**Fix:** Fernet encrypt fallback with machine-derived key.

### 6. Missing Dependencies — Fresh Install Will Crash
**File:** `pyproject.toml` — `authlib` and `httpx` imported in `auth/auth_manager.py` but **not declared**.  
**Fix:** Add to dependencies immediately.

### 7. Abandoned Dependency Listed
**File:** `pyproject.toml` — `ffmpeg-python>=0.2.0` (last release 2019, never imported).  
**Fix:** Remove from dependencies.

---

## 🟠 HIGH — Degrade Reliability/Maintainability

### 8. Engine.py = God Object (989 lines, 19 deps, 0% test coverage)
**AUDIT_REPORT.md** documents:
- 14 responsibilities in one class
- Zero tests for the most complex module
- Upload pipeline duplicated 3x (`check_and_post`, `post_manual`, `post_manual_carousel`)
- No dependency injection — impossible to unit test in isolation

**Fix:** Extract use-case classes (`FetchVideosUseCase`, `CrossPostUseCase`, `HealthCheckUseCase`, `BackfillUseCase`), add DI, write tests.

### 9. No Use-Case Layer (Clean Architecture Violation)
CLI/Dashboard → Engine directly. No intermediate service layer.  
**Fix:** Create `xpst/usecases/` with single-responsibility classes.

### 10. Scheduler.py Dead Code (Duplicates CLI Watch)
**File:** `src/xpst/scheduler.py` — Never used by CLI (CLI has its own watch loop at `cli.py:278-308`).  
**Fix:** Delete or fully integrate.

### 11. Auth Expiry Handling Inconsistent
Platform uploaders classify errors independently; UploadService ALSO classifies. Two conflicting layers.  
**Fix:** Remove classification from platforms; let UploadService handle it.

---

## 🟡 MEDIUM — Technical Debt

### 12. 37 Bare `except Exception:` Clauses
Silently swallow errors across 14 files (crash_recovery, desktop, upload_service, analytics, sources, cli, setup, connect, dashboard, updater).  
**Fix:** Add `as e` + `logger.debug(...)` minimum.

### 13. YouTube Upload Blocks Event Loop
**File:** `platforms/youtube.py:280-283` — Sync `request.next_chunk()` in async method.  
**Fix:** `await loop.run_in_executor(None, upload_blocking)`

### 14. TikTokSource Uses `subprocess.run()` in Async
**File:** `sources/tiktok.py:208,340,389,434` — Blocks event loop.  
**Fix:** `asyncio.create_subprocess_exec()`

### 15. 28 Functions Missing Return Type Annotations
Platforms, sources, dashboard analytics, sessions.py.  
**Fix:** Add proper type hints.

### 16. No Path Traversal Validation
Config paths accept arbitrary strings (`~/../../etc/passwd`).  
**Fix:** Validate in `_validate()` — reject paths outside home directory.

### 17. StateManager.save() Called Per-Video (15 disk writes/batch)
**File:** `engine.py:381` — Each save = backup rotation + copy + atomic rename.  
**Fix:** Batch saves / debounce.

### 18. QuotaManager.save() Called Per-Upload
**File:** `utils/quota.py:224` — Same I/O amplification.

### 19. Registry Classes Use Mutable Class-Level Dicts
**Files:** `platforms/base.py:190`, `sources/base.py:163` — Test pollution risk.  
**Fix:** Use `__init_subclass__` or instance registries.

---

## 🟢 LOW — Polish

### 20. Duplicate `@dataclass` on CrossPostResult
**File:** `engine.py:53-54` — Harmless but sloppy.

### 21. `CAPTION_PREFIXES` All Empty Strings
**File:** `anti_bot.py:89-118` — Dead code in `vary_caption()`.

### 22. Double Docstring on YouTubeUploader
**File:** `platforms/youtube.py:35-42`

### 23. `any` vs `Any` Type Annotation
**File:** `services/source_service.py:117` — `dict[str, any]` should be `dict[str, Any]`.

### 24. Legacy Crosspstr Migration Code
**File:** `config.py:308-315, 452-472` — Flag for removal in v1.0.

---

## Cross-Platform Issues

| Issue | Severity | Location |
|-------|----------|----------|
| Hardcoded `ffmpeg`/`ffprobe` strings (7 locations) | MEDIUM | video.py, instagram.py, setup.py, updater.py, progress.py |
| macOS Python 3.12 hardcoded in yt-dlp fallback | LOW | platform.py:55 |
| Windows desktop needs `winshell`/`pywin32` not in deps | MEDIUM | desktop.py:227-228 |
| Linux Qt fallback only tries PyQt5 | LOW | desktop.py:84-86 |
| Config dir inconsistency: APPDATA vs ~/.xpst | LOW | platform.py vs config.py |

---

## CLI Agent-Readiness Gaps

| Missing Feature | Impact |
|-----------------|--------|
| `--json` global flag | Agents can't parse Rich tables |
| Meaningful exit codes | Can't distinguish error types |
| `--quiet` flag | No bare output mode |
| `--dry-run` on post/run/backfill | Agents can't preview |
| TTY detection for interactive commands | Blocks in non-interactive shells |
| `xpst config get/set` | No programmatic config management |

---

## Test Coverage Reality

| Module | Lines | Coverage | Notes |
|--------|-------|----------|-------|
| engine.py | 389 | **33%** | Most complex module, 0% on core workflows |
| scheduler.py | ~50 | **0%** | Dead code |
| All other modules | — | 85%+ | Good |

**793 tests pass** but mostly test utils/platforms/sources — NOT the orchestration logic.

---

## Recommended Fix Order (Priority Stack)

```
WEEK 1 (P0 - CRITICAL):
1. Fix XUploader.delete() → async
2. Add authlib/httpx to pyproject.toml
3. Remove ffmpeg-python dependency
4. Hash dashboard password
5. Encrypt credential fallback

WEEK 2 (P0 - CRITICAL):
6. Add thread safety to StateManager (lock all mutations)
7. Consolidate auth loading → SessionManager only
8. Fix YouTube/TikTok async blocking

WEEK 3 (P1 - HIGH):
9. Extract use-case layer from engine
10. Add DI to CrossPostEngine
11. Write engine tests (target 80%+)
12. Delete scheduler.py or integrate

WEEK 4 (P1 - HIGH):
13. Add --json, --quiet, exit codes, --dry-run to CLI
14. Add path traversal validation
15. Fix all bare except: clauses
16. Fix cross-platform ffmpeg paths

WEEK 5+ (P2 - MEDIUM):
17. Replace config manual merge with pydantic-settings
18. Split StateManager into focused trackers
19. Build MCP server (stdio transport)
20. Add upper bounds on nicegui/plotly
```

---

## File Map for Quick Navigation

```
~/XPST/
├── AUDIT_REPORT.md           # Architecture audit (read first)
├── AUDIT_CODE_QUALITY.md     # Code quality audit
├── AUDIT_CROSSPLATFORM_DEPS.md  # Cross-platform + deps
├── AUDIT_MCP_CLI.md          # MCP + CLI agent audit
├── src/xpst/
│   ├── engine.py             # GOD OBJECT - 989 lines
│   ├── state.py              # Thread safety issues - 926 lines
│   ├── config.py             # Triple manual merge - 772 lines
│   ├── scheduler.py          # DEAD CODE - 138 lines
│   ├── cli.py                # 22 commands, no --json - 2765 lines
│   ├── platforms/
│   │   ├── x.py              # CRITICAL: delete() bug - 485 lines
│   │   ├── youtube.py        # Blocks event loop - 525 lines
│   │   └── instagram.py      # 350 lines
│   ├── services/
│   │   ├── upload_service.py # Good extraction - 535 lines
│   │   └── source_service.py
│   ├── utils/
│   │   ├── circuit_breaker.py
│   │   ├── credentials.py    # Unencrypted fallback
│   │   ├── video.py          # Hardcoded ffmpeg
│   │   └── sessions.py       # Underused - 368 lines
│   └── auth/auth_manager.py  # Imports missing deps
├── tests/
│   ├── test_engine.py        # Only 24 tests, 33% coverage
│   └── (27 other test files)
└── pyproject.toml            # Missing authlib, httpx
```

---

## Bottom Line

**xPST is a remarkable prototype with production-grade features** (desktop app, MCP, plugins, 793 tests). But the **core orchestration layer is architected like a script, not a system**. The God Object engine, missing thread safety, async bugs, and security gaps mean **it will fail under real-world concurrent load**.

Invest **2-3 weeks** on the P0/P1 items above → **enterprise-grade foundation**. Skip them → **technical bankruptcy in 6 months**.