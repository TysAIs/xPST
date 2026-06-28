# xPST Enterprise-Grade Transformation — Complete Phased Build Plan

**Generated:** 2026-06-09  
**Scope:** All 4 audit reports + full codebase review → Production-ready, free, open-source, secure, cross-platform  
**Total Phases:** 7 | **Estimated Effort:** ~30-40 development days

---

## Phase 0: Foundation & Audit Synthesis (COMPLETE)
**Status:** ✅ Done — All audit reports reviewed, codebase mapped, issues cataloged

---

## Phase 1: P0 Critical Fixes — "Stop the Bleeding" (5-7 days)

### 1.1 Add Missing Dependencies (`pyproject.toml`)
**Files:** `pyproject.toml`  
**Issue:** `authlib` and `httpx` imported in `auth/auth_manager.py` but not declared — fresh install crashes.

**Fix:**
```toml
dependencies = [
    # ... existing ...
    "authlib>=1.3.0",        # OAuth2 for YouTube desktop flow
    "httpx>=0.24.0",         # Required by authlib's OAuth2Client
]
```
**Validation:** `pip install -e .` in clean venv → no ImportError on `xpst auth youtube`

---

### 1.2 Remove Abandoned Dependency (`pyproject.toml`)
**Files:** `pyproject.toml`  
**Issue:** `ffmpeg-python>=0.2.0` — last release 2019, never imported, 7 years abandoned.

**Fix:** Remove from `dependencies` list.  
**Validation:** Full test suite passes (793 tests), no import of `ffmpeg_python` anywhere.

---

### 1.3 Fix XUploader.delete() — Already Async ✅
**Investigation:** Current code at `platforms/x.py:210-219` shows `async def delete()` — the audit was based on older code. **No fix needed.**  
**Validation:** Check `delete` signature in all 3 platforms — all are `async`.

---

### 1.4 Hash Dashboard Password (Security)
**Files:** `src/xpst/config.py`, `src/xpst/dashboard/server.py`  
**Issue:** `dashboard_password: str = ""` stored in plaintext YAML, serialized to disk.

**Fix:**
```python
# config.py - add to MonitoringConfig
import bcrypt

@dataclass
class MonitoringConfig:
    # ...
    dashboard_password_hash: str = ""  # bcrypt hash, not plaintext
    
    def set_password(self, plaintext: str) -> None:
        self.dashboard_password_hash = bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()
    
    def verify_password(self, plaintext: str) -> bool:
        if not self.dashboard_password_hash:
            return False
        return bcrypt.checkpw(plaintext.encode(), self.dashboard_password_hash.encode())

# server.py - update auth check
if not config.monitoring.verify_password(password):
    raise HTTPException(status_code=401)
```
**Add to pyproject.toml:** `bcrypt>=4.0.0`  
**Validation:** Config YAML shows hash, login works with plaintext, wrong password rejected.

---

### 1.5 Encrypt Credential Fallback (Security)
**Files:** `src/xpst/utils/credentials.py`  
**Issue:** Fallback stores plaintext JSON at line 89 — contradicts security docstring.

**Fix:** Add Fernet encryption for fallback files:
```python
# credentials.py - add at top
from cryptography.fernet import Fernet
import base64
import hashlib
import os

class CredentialStore:
    def __init__(self, config_dir: str = "~/.xpst"):
        # ... existing ...
        # Derive encryption key from machine-specific data
        self._fernet_key = self._derive_fernet_key()
        self._fernet = Fernet(self._fernet_key)
    
    def _derive_fernet_key(self) -> bytes:
        # Use machine-id + user home as key material (deterministic per machine)
        machine_id = ""
        for path in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
            try:
                machine_id = Path(path).read_text().strip()
                break
            except OSError:
                pass
        if not machine_id:
            machine_id = str(Path.home())
        key_material = hashlib.sha256(
            f"xpst-fallback-{machine_id}".encode()
        ).digest()
        return base64.urlsafe_b64encode(key_material)
    
    def store(self, key: str, value: str) -> None:
        if self._use_keyring:
            # ... existing keyring logic ...
            return
        # Encrypted fallback
        encrypted = self._fernet.encrypt(value.encode())
        cred_file = self.creds_dir / f"{key}.enc"
        cred_file.write_bytes(encrypted)
    
    def retrieve(self, key: str) -> str | None:
        if self._use_keyring:
            # ... existing keyring logic ...
            pass
        # Encrypted fallback
        cred_file = self.creds_dir / f"{key}.enc"
        if cred_file.exists():
            try:
                return self._fernet.decrypt(cred_file.read_bytes()).decode()
            except Exception:
                return None
        return None
```
**Add to pyproject.toml:** `cryptography>=41.0.0`  
**Validation:** Fallback files are `.enc` binary, unreadable without machine key; migrate_from_files() works.

---

### 1.6 YouTube Upload Blocks Event Loop (Async Fix)
**Files:** `src/xpst/platforms/youtube.py:272-277`  
**Issue:** Sync `request.next_chunk()` in `async def upload()` blocks event loop for entire upload.

**Fix:**
```python
# youtube.py - replace lines 272-277
import asyncio

# ... inside upload() ...
request = service.videos().insert(...)

# Run blocking upload in executor
loop = asyncio.get_event_loop()
response = await loop.run_in_executor(
    None,
    lambda: self._execute_upload(request)
)

def _execute_upload(self, request):
    """Blocking upload execution for thread pool."""
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            progress = int(status.progress() * 100)
            logger.info(f"YouTube upload: {progress}%")
    return response
```
**Validation:** Upload large video → other async tasks (health checks, other platform uploads) continue during upload.

---

### 1.7 TikTokSource Uses Sync Subprocess in Async (Async Fix)
**Files:** `src/xpst/sources/tiktok.py:208, 340, 389, 434` (4 locations)  
**Issue:** `subprocess.run()` blocks event loop during yt-dlp calls.

**Fix:** Replace all with `asyncio.create_subprocess_exec()`:
```python
# Replace subprocess.run() pattern:
# OLD:
# result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

# NEW:
proc = await asyncio.create_subprocess_exec(
    *cmd,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
try:
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
except asyncio.TimeoutError:
    proc.kill()
    await proc.wait()
    raise
```
**Validation:** Concurrent checks (e.g., `xpst run --bidirectional`) show parallel downloads in logs.

---

## Phase 2: P0 Thread Safety & Auth Consolidation (5-7 days)

### 2.1 StateManager Thread Safety — Lock All Mutations
**Files:** `src/xpst/state.py`  
**Issue:** 7 methods mutate `_state` without `_save_lock` (lines 449-552, 526-549, etc.)

**Fix:** Wrap EVERY state mutation in `with self._save_lock:`:
```python
# All these methods need the lock:
def mark_video_posted(self, ...):
    with self._save_lock:
        # ... existing logic ...

def mark_video_failed(self, ...):
    with self._save_lock:
        # ... existing logic ...

def update_platform_health(self, ...):
    with self._save_lock:
        # ... existing logic ...

def mark_cross_posted(self, ...):
    with self._save_lock:
        # ... existing logic ...

def mark_cross_post_failed(self, ...):
    with self._save_lock:
        # ... existing logic ...

def remove_post(self, ...):
    with self._save_lock:
        # ... existing logic ...

def clear_dead_letter_queue(self, ...):
    with self._save_lock:
        # ... existing logic ...
```
**Add regression test:** `tests/test_state_concurrency.py` — spawn 10 threads calling `mark_video_posted` simultaneously, verify no corruption.

---

### 2.2 Consolidate Auth Loading → SessionManager Only
**Problem:** 3 platform uploaders + 2 source modules duplicate credential loading (244 lines total):
- YouTubeUploader: 145 lines (`_get_credentials`, `_get_credentials_authlib`, `_get_credentials_google_auth`, `_get_service`)
- InstagramUploader: 63 lines (`_get_client`)
- XUploader: 36 lines (`_get_client`)
- InstagramSource: ~50 lines (similar logic)
- XSource: ~40 lines

**Solution:** Make ALL platform uploaders and sources delegate to `SessionManager`:

#### 2.2.1 YouTubeUploader → Use SessionManager.get_youtube_service()
```python
# youtube.py - DELETE lines 51-208 (_get_credentials, _get_credentials_authlib, _get_credentials_google_auth, _get_service)
# REPLACE _get_service() with:
async def _get_service(self):
    if self._service is None:
        self._service = await self._session_manager.get_youtube_service(
            self.config.youtube.client_secrets,
            self.config.youtube.token_file,
        )
    return self._service

# Update upload() to be fully async (already is)
# Update check_health() to use session_manager
```

#### 2.2.2 InstagramUploader/Source → Use SessionManager.get_instagram_client()
```python
# platforms/instagram.py - DELETE _get_client() (lines 54-117)
async def _get_client(self):
    if self._client is None:
        self._client = await self._session_manager.get_instagram_client(
            self.config.instagram.session_file,
            self.config.instagram.username,
            self.config.instagram.password,  # May need to add to config
        )
    return self._client

# sources/instagram.py - DELETE _get_client(), use session_manager
```

#### 2.2.3 XUploader/Source → Use SessionManager.get_x_client()
```python
# platforms/x.py - DELETE _get_client() (lines 44-80)
async def _get_client(self):
    if self._client is None:
        self._client = await self._session_manager.get_x_client(
            self.config.x.cookies_file,
            self.config.x.username,
            self.config.x.password,  # May need to add to config
        )
    return self._client

# sources/x.py - DELETE _get_client(), use session_manager
```

**Config additions needed:** Add `username`/`password` fields to InstagramAccountConfig and XAccountConfig for re-login support.

**Validation:** 
- All 3 platforms authenticate via SessionManager
- Token refresh works automatically
- No duplicate credential logic remains in platform/source files
- Tests pass

---

### 2.3 Registry Classes — Fix Mutable Class-Level Dicts (Test Safety)
**Files:** `src/xpst/platforms/base.py:190`, `src/xpst/sources/base.py:163`  
**Issue:** Class variables `_registry` shared across tests → test pollution.

**Fix:** Use `__init_subclass__` for auto-registration:
```python
# platforms/base.py
class PlatformUploader(ABC):
    _registry: dict[str, type[PlatformUploader]] = {}
    
    def __init_subclass__(cls, platform_name: str | None = None, **kwargs):
        super().__init_subclass__(**kwargs)
        if platform_name:
            cls._registry[platform_name] = cls

# Usage in each platform:
class YouTubeUploader(PlatformUploader, platform_name="youtube"):
    ...

# sources/base.py - same pattern
class VideoSource(ABC):
    _registry: dict[str, type[VideoSource]] = {}
    
    def __init_subclass__(cls, source_name: str | None = None, **kwargs):
        super().__init_subclass__(**kwargs)
        if source_name:
            cls._registry[source_name] = cls
```

**Add test isolation:** `pytest` fixture to clear registries between tests.

---

## Phase 3: P1 Architecture Refactor — Use-Case Layer (7-10 days)

### 3.1 Create `xpst/usecases/` Package
```
src/xpst/usecases/
├── __init__.py
├── base.py                    # BaseUseCase abstract class
├── fetch_videos.py           # FetchNewVideosUseCase
├── cross_post.py             # CrossPostVideoUseCase
├── manual_post.py            # PostManualUseCase
├── backfill.py               # BackfillUseCase
├── health_check.py           # HealthCheckUseCase
├── delete_post.py            # DeletePostUseCase
└── bidirectional.py          # BidirectionalCrossPostUseCase
```

### 3.2 Base Use-Case Class (Dependency Injection)
```python
# usecases/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol

class UseCaseDependencies(Protocol):
    """Protocol for use-case dependencies — enables DI and testing."""
    config: Any
    state: Any
    video_processor: Any
    circuit_breakers: Any
    quota_manager: Any
    notifier: Any
    shutdown_handler: Any
    session_manager: Any
    upload_service: Any
    source_service: Any
    crash_recovery: Any
    anti_bot: Any
    platforms: dict[str, Any]
    sources: dict[str, Any]

@dataclass
class UseCaseResult:
    success: bool
    data: Any = None
    error: str | None = None

class BaseUseCase(ABC):
    def __init__(self, deps: UseCaseDependencies):
        self.deps = deps
    
    @abstractmethod
    async def execute(self, *args, **kwargs) -> UseCaseResult:
        pass
```

### 3.3 Extract Engine Responsibilities → Use Cases

| Engine Method | → Use Case | Deps Needed |
|---------------|------------|-------------|
| `check_and_post()` | `FetchNewVideosUseCase` + `CrossPostVideoUseCase` | source_service, upload_service, state, circuit_breakers, quota, notifier, shutdown, crash_recovery, anti_bot, platforms |
| `check_and_post_bidirectional()` | `BidirectionalCrossPostUseCase` | monitor, upload_service, state, anti_bot, platforms, sources |
| `post_manual()` | `PostManualUseCase` | upload_service, platforms, state |
| `post_manual_carousel()` | `PostManualCarouselUseCase` | upload_service, platforms, state |
| `backfill()` | `BackfillUseCase` | state, upload_service, platforms |
| `delete_post()` | `DeletePostUseCase` | state, platforms |
| `check_health()` | `HealthCheckUseCase` | state, platforms, sources, circuit_breakers, quota |

### 3.4 Refactor CrossPostEngine → Orchestrator Only
```python
# engine.py - NEW slim version (~200 lines)
class CrossPostEngine:
    def __init__(
        self, 
        config: XPSTConfig,
        # DI - all optional for backward compat
        state: StateManager | None = None,
        video_processor: VideoProcessor | None = None,
        circuit_breakers: CircuitBreakerManager | None = None,
        credentials: CredentialStore | None = None,
        session_manager: SessionManager | None = None,
        quota_manager: QuotaManager | None = None,
        notifier: WebhookNotifier | None = None,
        shutdown_handler: ShutdownHandler | None = None,
        crash_recovery: CrashRecoveryManager | None = None,
        upload_service: UploadService | None = None,
        source_service: SourceService | None = None,
        anti_bot: AntiBotProtection | None = None,
    ):
        # Initialize with DI or create defaults
        self.config = config
        self.state = state or StateManager(config.config_dir)
        self.video_processor = video_processor or VideoProcessor()
        self.circuit_breakers = circuit_breakers or CircuitBreakerManager()
        self.credentials = credentials or CredentialStore(config.config_dir)
        self.session_manager = session_manager or SessionManager(config.config_dir)
        self.quota_manager = quota_manager or QuotaManager(config.config_dir)
        
        # Notifier needs config mapping
        self.notifier = notifier or WebhookNotifier(NotificationConfig(
            enabled=config.notifications.enabled,
            on_success=config.notifications.on_success,
            on_failure=config.notifications.on_failure,
            discord_webhook_url=config.notifications.discord_webhook_url,
            telegram_bot_token=config.notifications.telegram_bot_token,
            telegram_chat_id=config.notifications.telegram_chat_id,
        ))
        
        self.shutdown_handler = shutdown_handler or ShutdownHandler(config.config_dir)
        self.shutdown_handler.register()
        
        self.crash_recovery = crash_recovery or CrashRecoveryManager(config.config_dir)
        self.anti_bot = anti_bot or AntiBotProtection(...)
        
        # Services
        self.source_service = source_service or SourceService(config)
        self.upload_service = upload_service or UploadService(
            video_processor=self.video_processor,
            circuit_breakers=self.circuit_breakers,
            quota_manager=self.quota_manager,
            state=self.state,
            notifier=self.notifier,
            shutdown_handler=self.shutdown_handler,
            config=config,
            anti_bot=self.anti_bot,
        )
        self.upload_service._crash_recovery = self.crash_recovery
        
        # Platforms & sources
        self._sources = self.source_service.sources
        self._platforms = {}
        self._init_platforms()
        for p in self._platforms.values():
            p._session_manager = self.session_manager
        
        self._startup_crash_recovery()
    
    # Thin delegation methods
    async def check_and_post(self, catch_up: bool = False):
        uc = FetchNewVideosUseCase(self._make_deps())
        return await uc.execute(catch_up=catch_up)
    
    def _make_deps(self) -> UseCaseDependencies:
        return self  # Engine implements the protocol
```

### 3.5 Write Engine Tests (Target: 80%+ Coverage)
**File:** `tests/test_engine.py` (expand from 24 → ~80 tests)

**Test Categories:**
- `FetchNewVideosUseCase`: source fetch, filtering, catch-up logic
- `CrossPostVideoUseCase`: download → encode → upload pipeline, circuit breaker, quota, shutdown mid-flow
- `BidirectionalCrossPostUseCase`: multi-source fetch, target order randomization, anti-bot delays
- `PostManualUseCase`: single video, carousel, platform selection, dry-run
- `BackfillUseCase`: finds missing platforms, handles missing files
- `DeletePostUseCase`: succeeds, handles not-found, auth expiry
- `HealthCheckUseCase`: all sources/platforms, circuit breakers, quotas

**Mock Strategy:** Use real StateManager, CircuitBreaker, QuotaManager; mock only external APIs (platform uploaders, sources).

---

### 3.6 Delete or Integrate `scheduler.py`
**Decision:** DELETE — CLI watch loop at `cli.py:278-308` is the canonical implementation. Scheduler duplicates logic and is never used.

**Action:** Remove `src/xpst/scheduler.py`, remove import from `cli.py`, update any references.

---

## Phase 4: P1 CLI Agent-Readiness (5-7 days)

### 4.1 Global `--json`, `--quiet`, Exit Codes
**Files:** `src/xpst/cli.py` (major refactor)

**Add to main Click group:**
```python
@click.group()
@click.option("--json", "as_json", is_flag=True, help="Output machine-readable JSON")
@click.option("--quiet", "-q", is_flag=True, help="Suppress decorative output")
@click.option("--dry-run", is_flag=True, help="Preview actions without executing")
@click.pass_context
def main(ctx, as_json: bool, quiet: bool, dry_run: bool):
    ctx.ensure_object(dict)
    ctx.obj["json_output"] = as_json
    ctx.obj["quiet"] = quiet
    ctx.obj["dry_run"] = dry_run
    # TTY detection
    ctx.obj["interactive"] = sys.stdin.isatty()
```

**Exit Code Enum:**
```python
# cli.py - add at top
class ExitCode(IntEnum):
    SUCCESS = 0
    GENERAL_ERROR = 1
    USAGE_ERROR = 2
    AUTH_ERROR = 3
    PLATFORM_API_ERROR = 4
    RATE_LIMITED = 5
    QUOTA_EXCEEDED = 6
    CONFIG_ERROR = 7
    NETWORK_ERROR = 8

def exit_with(code: ExitCode, message: str = "", json_data: dict | None = None):
    if ctx.obj.get("json_output"):
        result = {"exit_code": code, "message": message}
        if json_data:
            result.update(json_data)
        click.echo(json.dumps(result))
    elif message and not ctx.obj.get("quiet"):
        click.echo(message)
    sys.exit(code)
```

### 4.2 Every Command: JSON Branch + Meaningful Exit Codes
**Pattern for each command:**
```python
@main.command()
@click.option(...)
@click.pass_context
def run(ctx, ...):
    try:
        engine = CrossPostEngine(config)
        results = await engine.check_and_post()
        
        if ctx.obj["json_output"]:
            json_output = {
                "videos_processed": len(results),
                "results": [r.to_dict() for r in results],  # Add to_dict() to CrossPostResult
            }
            exit_with(ExitCode.SUCCESS, json_data=json_output)
        else:
            # Existing Rich display
            for r in results: _display_result(r)
            exit_with(ExitCode.SUCCESS)
    except AuthError as e:
        exit_with(ExitCode.AUTH_ERROR, f"Authentication failed: {e}")
    except QuotaExceeded as e:
        exit_with(ExitCode.QUOTA_EXCEEDED, f"Quota exceeded: {e}")
    except PlatformAPIError as e:
        exit_with(ExitCode.PLATFORM_API_ERROR, f"Platform error: {e}")
    except Exception as e:
        exit_with(ExitCode.GENERAL_ERROR, f"Error: {e}")
```

### 4.3 TTY Detection for Interactive Commands
**Files:** `cli.py` — `connect`, `setup`, `auth`, `delete`

```python
# Replace click.confirm() with TTY-aware version
def confirm_or_fail(ctx, message: str, require_yes: bool = False) -> bool:
    if not ctx.obj.get("interactive"):
        if require_yes:
            exit_with(ExitCode.USAGE_ERROR, 
                "Running non-interactively but confirmation required. Use --yes flag.")
        return True  # Default to yes in non-interactive for backward compat
    return click.confirm(message)

# In delete command:
if not yes and not confirm_or_fail(ctx, f"Delete {video_id}...", require_yes=True):
    exit_with(ExitCode.USAGE_ERROR, "Aborted by user")
```

### 4.4 `--dry-run` for Mutating Commands
**Commands:** `post`, `run`, `backfill`, `delete`

```python
if ctx.obj.get("dry_run"):
    # Return what WOULD happen
    plan = {
        "action": "post",
        "video": str(video_path),
        "caption": caption[:100],
        "targets": targets,
        "estimated_operations": len(targets),
    }
    exit_with(ExitCode.SUCCESS, json_data=plan)
```

### 4.5 `xpst config get/set` Subcommands
```python
@config_group.command("get")
@click.argument("key")
@click.pass_context
def config_get(ctx, key: str):
    """Get config value by dot-notation key (e.g., 'video.download_dir')"""
    value = get_nested_attr(config, key)
    if ctx.obj["json_output"]:
        exit_with(ExitCode.SUCCESS, json_data={key: value})
    else:
        click.echo(value)

@config_group.command("set")
@click.argument("key")
@click.argument("value")
@click.pass_context
def config_set(ctx, key: str, value: str):
    """Set config value and save"""
    set_nested_attr(config, key, parse_value(value))
    config.save()
    exit_with(ExitCode.SUCCESS)
```

---

## Phase 5: P2 Cross-Platform & Polish (3-5 days)

### 5.1 Fix Hardcoded ffmpeg/ffprobe Paths
**Files:** 7 locations across 5 files
- `video.py:35, 78` — use `get_ffmpeg_name()` / `get_ffprobe_name()`
- `instagram.py:148` — use `get_ffmpeg_name()`
- `setup.py:42, 119` — use `get_ffmpeg_name()`
- `updater.py:262, 265` — use `get_ffmpeg_name()`
- `progress.py:245` — use `get_ffprobe_name()`

**Better fix:** Cache `shutil.which()` at startup in `platform.py`:
```python
# platform.py
_cached_ffmpeg = None
_cached_ffprobe = None

def get_ffmpeg_path() -> str:
    global _cached_ffmpeg
    if _cached_ffmpeg is None:
        _cached_ffmpeg = shutil.which("ffmpeg") or get_ffmpeg_name()
    return _cached_ffmpeg

def get_ffprobe_path() -> str:
    global _cached_ffprobe
    if _cached_ffprobe is None:
        _cached_ffprobe = shutil.which("ffprobe") or get_ffprobe_name()
    return _cached_ffprobe
```

### 5.2 Fix macOS yt-dlp Fallback Path
**File:** `platform.py:55`
```python
# OLD: hardcoded "3.12"
# NEW:
import sys
def get_ytdlp_fallback_path() -> Path:
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return Path.home() / "Library" / "Python" / version / "bin" / "yt-dlp"
```

### 5.3 Windows Desktop Dependencies
**File:** `pyproject.toml`
```toml
[project.optional-dependencies]
windows = [
    "pywin32>=306; platform_system == 'Windows'",
    "winshell>=0.6; platform_system == 'Windows'",
]
desktop = [
    "pywebview>=4.0",
    "xpst[windows]",  # Include windows deps
]
```

### 5.4 Linux Qt Fallback — Try PyQt6/PySide6
**File:** `desktop.py:84-86`
```python
# Replace PyQt5-only with cascade:
def _get_linux_backend() -> str:
    for backend in ["qt6", "qt5", "gtk"]:
        try:
            if backend == "qt6":
                from PyQt6 import QtWidgets  # noqa
                return "qt6"
            elif backend == "qt5":
                from PyQt5 import QtWidgets  # noqa
                return "qt"
            else:
                import gi  # noqa
                gi.require_version("Gtk", "3.0")
                from gi.repository import Gtk  # noqa
                return "gtk"
        except ImportError:
            continue
    return "auto"  # fallback to pywebview auto-detect
```

### 5.5 Config Directory Consistency
**File:** `config.py` — use `get_config_dir()` from `platform.py`:
```python
# config.py - XPSTConfig.config_dir default
from xpst.utils.platform import get_config_dir

@dataclass
class XPSTConfig:
    config_dir: str = field(default_factory=lambda: str(get_config_dir()))
```

### 5.6 Replace Config Manual Merge → pydantic-settings
**File:** `config.py` — Replace 23-line `_merge_config()` + 77-line `_apply_env_vars()` + 120-line `save()` with:
```toml
# pyproject.toml
dependencies = [
    # ... 
    "pydantic-settings>=2.0.0",
]
```
```python
# config.py - new approach
from pydantic_settings import BaseSettings, SettingsConfigDict

class XPSTConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="XPST_",
        env_nested_delimiter="__",
        yaml_file="~/.xpst/config.yaml",
        extra="ignore",
    )
    
    # All fields become auto-mapped from YAML + ENV
    tiktok: TikTokAccountConfig = TikTokAccountConfig()
    youtube: YouTubeAccountConfig = YouTubeAccountConfig()
    # ... etc
    
    # Validation
    @field_validator("video.download_dir", "monitoring.log_file", ...)
    @classmethod
    def validate_paths(cls, v: str) -> str:
        Path(v).expanduser().mkdir(parents=True, exist_ok=True)
        return v
```

---

## Phase 6: P3 Enterprise Hardening (5-7 days)

### 6.1 Build MCP Server (stdio transport)
**File:** `src/xpst/mcp_server.py`
```python
from mcp.server.fastmcp import FastMCP
from xpst.engine import CrossPostEngine
from xpst.config import XPSTConfig

mcp = FastMCP("xPST")

@mcp.tool()
async def post_video(video_path: str, caption: str, platforms: list[str] | None = None) -> dict:
    config = XPSTConfig.load()
    engine = CrossPostEngine(config)
    result = await engine.post_manual(Path(video_path), caption, platforms)
    return result.to_dict()

@mcp.tool()
async def crosspost_new(bidirectional: bool = False, limit: int = 10) -> list[dict]:
    config = XPSTConfig.load()
    engine = CrossPostEngine(config)
    if bidirectional:
        results = await engine.check_and_post_bidirectional()
    else:
        results = await engine.check_and_post()
    return [r.to_dict() for r in results]

@mcp.tool()
async def check_status() -> dict:
    config = XPSTConfig.load()
    engine = CrossPostEngine(config)
    return await engine.check_health()

@mcp.tool()
async def list_platforms() -> dict:
    config = XPSTConfig.load()
    engine = CrossPostEngine(config)
    return {
        platform: {
            "enabled": True,
            "health": await engine._platforms[platform].check_health()
        }
        for platform in engine._platforms
    }

# ... 6 more tools: get_analytics, schedule_post, delete_post, retry_failed, get_logs, health_check, manage_auth

@mcp.resource("xpst://config")
async def get_config() -> str:
    config = XPSTConfig.load()
    return config.to_yaml()  # Sanitized (no secrets)

@mcp.resource("xpst://state")
async def get_state() -> str:
    config = XPSTConfig.load()
    engine = CrossPostEngine(config)
    return engine.state.to_json()

# ... 5 more resources

def main():
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()
```

**Add to pyproject.toml:**
```toml
[project.scripts]
xpst-mcp = "xpst.mcp_server:main"

[project.optional-dependencies]
mcp = ["mcp[cli]>=1.0.0"]
```

**Claude Desktop config example in docs:**
```json
{
  "mcpServers": {
    "xpst": {
      "command": "xpst-mcp"
    }
  }
}
```

---

### 6.2 Observability Stack
| Component | Implementation |
|-----------|----------------|
| **Structured Logging** | `structlog` already configured — add JSON renderer for production |
| **Metrics** | `prometheus-client` — expose `/metrics` on dashboard (already wired) |
| **Health Endpoint** | `GET /healthz` on dashboard server — checks all platform connectivity |
| **Distributed Tracing** | Add `opentelemetry` optional dep for future |

---

### 6.3 Add Upper Bounds on Volatile Dependencies
**File:** `pyproject.toml`
```toml
dependencies = [
    # ...
    "nicegui>=1.4.0,<4.0.0",   # Major API changes between 1.x and 3.x
    "plotly>=5.18.0,<7.0.0",   # Major version gap
]
```

---

### 6.4 Security Hardening Checklist
- [ ] Path traversal validation on ALL config paths (download_dir, log_file, session_file, cookies_file, token_file, client_secrets)
- [ ] Dashboard: rate limit login attempts, secure session cookies (HttpOnly, Secure, SameSite)
- [ ] Dashboard: CSP headers
- [ ] Audit all `subprocess.run()` calls for shell injection (currently clean — all use list form)
- [ ] Sanitize all user inputs in CLI (video paths, captions, platform names)

---

### 6.5 Release Automation
**Files:** `scripts/sign_macos.sh`, `scripts/release.sh` — enhance:
```bash
# scripts/release.sh
#!/usr/bin/env bash
set -euo pipefail

# 1. Run full test suite
pytest --cov=xpst --cov-fail-under=80

# 2. Type check
mypy src/xpst

# 3. Lint
ruff check src/xpst

# 4. Build wheel
python -m build

# 5. Sign macOS binary (ad-hoc)
./scripts/sign_macos.sh dist/xpst-*.dmg

# 6. Create GitHub release with artifacts
gh release create v$VERSION dist/* --generate-notes

# 7. Publish to PyPI
twine upload dist/*
```

---

## Phase 7: Validation & Sign-Off (3-5 days)

### 7.1 Full Test Suite + Coverage Gate
```bash
pytest --cov=xpst --cov-fail-under=80 --cov-report=term-missing
# Target: 80% overall, 80% on engine/usecases
```

### 7.2 Integration Tests (Real APIs)
**New test file:** `tests/test_integration_real.py` (manual/scheduled)
- YouTube: upload → verify on channel → delete
- Instagram: upload Reel → verify → delete
- X: upload video → verify → delete
- TikTok source: fetch recent → download → verify file
- Cross-post: single video → all 3 platforms
- Bidirectional: post to Insta → cross-post to YT/X

### 7.3 Concurrency/Load Tests
**New test file:** `tests/test_stress_concurrent.py`
- 10 concurrent `check_and_post()` calls (simulate scheduler + CLI + dashboard)
- StateManager concurrent writes (100 threads)
- QuotaManager concurrent increments
- CircuitBreaker concurrent failures

### 7.4 Cross-Platform Validation Matrix
| Platform | Test Command | Expected |
|----------|---------------|----------|
| macOS (Apple Silicon) | `pytest`, `xpst run`, `xpst app` | All pass, app launches |
| macOS (Intel) | Same | All pass |
| Linux (Ubuntu 22.04+) | `pytest`, `xpst run`, `xpst dashboard` | All pass |
| Windows 10/11 | `pytest`, `xpst run`, `xpst app` | All pass, shortcuts created |

### 7.5 Security Audit
- [ ] No plaintext secrets in config YAML
- [ ] Keyring fallback encrypted
- [ ] Dashboard password hashed
- [ ] Path traversal blocked
- [ ] No shell injection vectors
- [ ] Dependencies scanned (`pip-audit` or `ghapi`)

### 7.6 Documentation Finalization
| Doc | Status |
|-----|--------|
| `README.md` | ✅ Current |
| `docs/ARCHITECTURE.md` | Update with use-case layer diagram |
| `docs/INSTALL.md` | Add Windows/Linux specifics |
| `docs/AGENT_GUIDE.md` | Document `--json`, exit codes, MCP |
| `docs/MCP_TOOLS.md` | Document all 10 tools + 7 resources |
| `docs/CONTRIBUTING.md` | Add development setup, test commands |
| `CHANGELOG.md` | v0.2.0 release notes |

---

## Dependency Summary (Final)

### Required (Core)
```toml
dependencies = [
    "click>=8.1.0",
    "pyyaml>=6.0",
    "rich>=13.0.0",
    "yt-dlp>=2025.1.1",
    "google-api-python-client>=2.0.0",
    "google-auth-oauthlib>=1.0.0",
    "google-auth-httplib2>=0.1.0",
    "twikit>=2.0.0",
    "instagrapi>=2.0.0",
    "structlog>=23.0.0",
    "prometheus-client>=0.19.0",
    "keyring>=25.0.0",
    "fastapi>=0.100.0",
    "uvicorn>=0.23.0",
    "authlib>=1.3.0",        # ← ADDED
    "httpx>=0.24.0",         # ← ADDED
    "bcrypt>=4.0.0",         # ← ADDED (dashboard password)
    "cryptography>=41.0.0",  # ← ADDED (credential fallback encryption)
    "pydantic-settings>=2.0.0",  # ← ADDED (config management)
]
```

### Optional Extras
```toml
[project.optional-dependencies]
mcp = ["mcp[cli]>=1.0.0"]
dashboard = ["nicegui>=1.4.0,<4.0.0", "plotly>=5.18.0,<7.0.0"]
pyside6 = ["PySide6>=6.5.0"]
desktop = ["pywebview>=4.0", "xpst[windows]"]
windows = ["pywin32>=306; platform_system == 'Windows'", "winshell>=0.6; platform_system == 'Windows'"]
dev = ["pytest>=7.0.0", "pytest-cov>=4.0.0", "pytest-asyncio>=0.21.0", "ruff>=0.1.0", "mypy>=1.0.0", "types-PyYAML>=6.0.0"]
full = ["xpst[mcp,dashboard,pyside6,desktop,dev]"]
```

---

## Success Criteria (Definition of Done)

| Criterion | Target |
|-----------|--------|
| **Test coverage** | ≥80% overall, ≥80% on engine/usecases |
| **All 793+ tests pass** | ✅ |
| **Fresh install works** | `pip install xpst` → `xpst --help` works |
| **CLI agent-ready** | `--json`, exit codes, `--quiet`, `--dry-run` on all commands |
| **MCP server works** | `xpst-mcp` connects to Claude Desktop |
| **Thread-safe** | Concurrent stress tests pass |
| **Secure** | No plaintext secrets, hashed passwords, encrypted fallback |
| **Cross-platform** | macOS/Linux/Windows CI passes |
| **Async-correct** | No blocking calls in async paths |
| **Architecture** | Clean use-case layer, DI, no God Objects |
| **Documentation** | Complete for users, developers, AI agents |

---

## Phase Dependency Graph

```
Phase 1 (P0 Critical) ──────────────────────┐
    │                                        │
    ▼                                        ▼
Phase 2 (P0 Thread Safety + Auth) ◄────── Phase 3 (P1 Architecture)
    │                                        │
    ▼                                        │
Phase 4 (P1 CLI) ◄──────────────────────────┘
    │
    ▼
Phase 5 (P2 Cross-Platform)
    │
    ▼
Phase 6 (P3 MCP + Observability + Release)
    │
    ▼
Phase 7 (Validation) ───► RELEASE v0.2.0
```

**Total Estimated: 33-48 days (6-9 weeks, 1 dev)**

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Authlib OAuth2 breaks YouTube desktop flow | Medium | High | Test early; fallback to google-auth-oauthlib preserved |
| pydantic-settings migration breaks config | Low | High | Keep old config as fallback; comprehensive config tests |
| SessionManager async refactor breaks platforms | Medium | High | Incremental: one platform at a time, full integration tests |
| MCP server scope creep | Medium | Medium | Strict 10-tool / 7-resource scope; defer advanced features |
| Windows desktop build fails | Medium | Medium | CI on Windows runner; optional desktop extra |
| Cross-platform ffmpeg issues | Low | Medium | Cache `shutil.which()` at startup; clear error messages |

---

This plan addresses **every single issue** from all 4 audit reports plus code review findings. Each phase builds on the previous, with clear validation gates. The end result: a production-grade, enterprise-worthy, free & open-source cross-posting solution.