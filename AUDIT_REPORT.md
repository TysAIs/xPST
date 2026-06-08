# XPST Comprehensive Codebase Audit Report
Generated: 2026-06-07

## Executive Summary

XPST is a 39-module, 14,423-line Python codebase for cross-posting short-form video across TikTok, YouTube, X/Twitter, and Instagram. The architecture is **fundamentally sound** at the layer level but suffers from a critical **God Object** in `engine.py`, **missing abstractions** (no use-case/service layer), and **code duplication** that undermines maintainability. The most impactful flaw is that the central engine module — the most complex module in the project — has **zero test coverage**.

**Severity Rating:** ⚠️ MODERATE — functional but fragile at scale.

---

## 1. Module Inventory (39 modules, 14,423 lines)

### CORE (10 modules, 4,972 lines)
| Module | Lines | Classes | Functions | Cyclomatic | Deps |
|--------|-------|---------|-----------|------------|------|
| `__init__` | 35 | 0 | 0 | 1 | 0 |
| `cli` | 857 | 0 | 25 | 88 | 12 |
| `config` | 659 | 13 | 6 | 84 | 0 |
| `connect` | 683 | 0 | 10 | 84 | 3 |
| `crash_recovery` | 227 | 1 | 9 | 20 | 1 |
| **`engine`** | **1,045** | **2** | **14** | **92** | **19** |
| `scheduler` | 179 | 1 | 7 | 14 | 3 |
| `setup` | 428 | 0 | 13 | 31 | 2 |
| `state` | 553 | 1 | 24 | 51 | 0 |
| `updater` | 306 | 1 | 8 | 43 | 2 |

### PLATFORMS (5 modules, 1,205 lines)
| Module | Lines | Classes | Functions | Deps |
|--------|-------|---------|-----------|------|
| `base` | 226 | 4 | 12 | 3 |
| `instagram` | 350 | 1 | 6 | 3 |
| `x` | 313 | 1 | 6 | 3 |
| `youtube` | 311 | 1 | 6 | 3 |

### SOURCES (7 modules, 2,622 lines)
| Module | Lines | Classes | Functions | Deps |
|--------|-------|---------|-----------|------|
| `base` | 226 | 5 | 13 | 2 |
| `tiktok` | 525 | 1 | 10 | 4 |
| `youtube` | 429 | 1 | 10 | 4 |
| `x` | 485 | 1 | 11 | 4 |
| `instagram` | 483 | 1 | 11 | 3 |
| `local` | 417 | 1 | 12 | 3 |

### UTILS (13 modules, 3,659 lines)
| Module | Lines | Classes | Functions | Deps |
|--------|-------|---------|-----------|------|
| `video` | 497 | 1 | 9 | 2 |
| `circuit_breaker` | 312 | 4 | 22 | 0 |
| `errors` | 347 | 2 | 7 | 0 |
| `notifications` | 371 | 4 | 15 | 1 |
| `sessions` | 368 | 1 | 7 | 2 |
| `shutdown` | 345 | 2 | 17 | 1 |
| `retry` | 320 | 1 | 8 | 2 |
| `credentials` | 279 | 1 | 12 | 1 |
| `progress` | 282 | 2 | 11 | 1 |
| `quota` | 265 | 2 | 15 | 1 |
| `logger` | 157 | 0 | 3 | 0 |
| `platform` | 92 | 0 | 8 | 0 |

### DASHBOARD (4 modules, 1,965 lines)
| Module | Lines | Classes | Functions | Deps |
|--------|-------|---------|-----------|------|
| `app` | 1,310 | 0 | 24 | 1 |
| `analytics` | 600 | 1 | 18 | 0 |
| `server` | 44 | 0 | 1 | 1 |

---

## 2. Dependency Graph

```
                    ┌──────────────────────────────┐
                    │        ENTRY POINTS           │
                    │  cli (857)  dashboard (1,965) │
                    │  setup (428) connect (683)    │
                    └──────────┬───────────────────┘
                               │
                    ┌──────────▼───────────────────┐
                    │     ORCHESTRATION             │
                    │  engine (1,045) ← GOD OBJECT  │
                    │  scheduler (179)  updater(306)│
                    │  crash_recovery (227)         │
                    └──────────┬───────────────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
    ┌─────────▼──────┐ ┌──────▼───────┐ ┌──────▼──────┐
    │  PLATFORMS     │ │   SOURCES    │ │    STATE     │
    │  base (226)    │ │  base (226)  │ │  state (553) │
    │  youtube (311) │ │  tiktok (525)│ │  config (659)│
    │  x (313)       │ │  x (485)     │ │              │
    │  instagram (350│ │  yt (429)    │ │              │
    │                │ │  ig (483)    │ │              │
    │                │ │  local (417) │ │              │
    └────────┬───────┘ └──────┬───────┘ └─────────────┘
             │                │
    ┌────────▼────────────────▼──────────────────────┐
    │                  UTILITIES                       │
    │  video (497)  circuit_breaker (312)              │
    │  errors (347)  notifications (371)               │
    │  sessions (368)  shutdown (345)                  │
    │  retry (320)  credentials (279)                  │
    │  progress (282)  quota (265)                     │
    │  logger (157)  platform (92)                     │
    └────────────────────────────────────────────────┘
```

### Critical Dependency Chains

**engine.py** imports **19 internal modules** — it is the single most coupled module:
```
engine → config, crash_recovery, state,
         platforms.base, platforms.instagram, platforms.x, platforms.youtube,
         sources.base, sources.tiktok,
         utils.circuit_breaker, utils.credentials, utils.logger,
         utils.notifications, utils.progress, utils.quota,
         utils.retry, utils.sessions, utils.shutdown, utils.video
```

**cli.py** imports **12 internal modules**:
```
cli → config, connect, crash_recovery, dashboard.server,
      engine, setup, state, updater,
      utils.credentials, utils.logger, utils.quota, utils.sessions
```

**Most imported module:** `utils.logger` (24 dependents — expected, used everywhere)

**config** has **16 dependents** — the most depended-on non-utility module.

---

## 3. Architectural Flaws Found

### 🔴 CRITICAL FLAWS

#### 1. GOD OBJECT: `engine.py` (1,045 lines, 19 dependencies, cc=92)
**Location:** `src/xpst/engine.py:85` (CrossPostEngine class)

The engine simultaneously handles:
- Source initialization (L180-229)
- Platform initialization (L196-229)
- Video filtering logic (L336-363)
- Download orchestration (L396-407)
- Platform encoding (L578-633)
- Upload with retry (L494-568)
- Circuit breaker management (L438, L526, L536)
- Quota management (L460, L528, L705)
- State persistence (L518-540)
- Notification dispatch (L299-311)
- Crash recovery (L410-411, L516)
- Shutdown coordination (L250-253, L290, L420)
- Manual posting (L651-786)
- Carousel posting (L788-908)
- Backfill logic (L910-964)
- Health checks (L998-1045)
- Post deletion (L966-996)

**Impact:** Any change to one subsystem risks breaking others. Impossible to test in isolation.

#### 2. NO USE-CASE LAYER
**Missing abstraction between CLI/Dashboard and Engine.**

There are no intermediate service/use-case classes. CLI and Dashboard must create the entire `CrossPostEngine` (which creates ALL dependencies) just to do one thing. This violates the Single Responsibility Principle and makes the code brittle.

#### 3. ENGINE HAS ZERO TESTS
**Location:** `tests/` — no `test_engine.py` exists

The most complex module (1,045 lines, cc=92) has no test coverage at all. `scheduler.py` also has no tests. Every other module is tested.

#### 4. DUPLICATE `@dataclass` DECORATOR (Bug)
**Location:** `src/xpst/engine.py:46-47`
```python
@dataclass   # ← duplicate, harmless but indicates sloppy editing
@dataclass
class CrossPostResult:
```

### 🟠 HIGH SEVERITY

#### 5. CODE DUPLICATION IN ENGINE: Upload Pipeline Repeated 3 Times
The pattern: check circuit breaker → check quota → encode → upload → record result → notify
is repeated in:
- `check_and_post()` (L438-568) — automated pipeline
- `post_manual()` (L694-783) — manual single upload
- `post_manual_carousel()` (L830-904) — manual carousel upload

Each copy is ~100 lines. This should be extracted to a single `_upload_to_platform()` method.

**Evidence:** 13 calls to `circuit_breakers.allow_request/record_*` across the file.

#### 6. STATE MANAGER VIOLATES SRP
**Location:** `src/xpst/state.py:386-410`

`StateManager.is_circuit_breaker_open()` contains business logic (1-hour timeout reset). State managers should only persist/read data, not make health-check decisions. This duplicates `utils/circuit_breaker.py` logic.

#### 7. `config.py` — 13 Classes, 659 Lines, Manual Merging
**Location:** `src/xpst/config.py:314-415`

`_merge_config()` contains **23 `.get()` calls** — each a potential source of type errors. A config library (pydantic-settings, dynaconf) would eliminate this entire method.

#### 8. `NotificationConfig` DEFINED TWICE
**Location:**
- `src/xpst/config.py:230` — `class NotificationConfig` (dataclass)
- `src/xpst/utils/notifications.py:49` — `class NotificationConfig` (regular class)

Two different classes with the same name. `engine.py:148-155` manually maps between them.

### 🟡 MEDIUM SEVERITY

#### 9. SCHEDULER DUPLICATES CLI WATCH LOGIC
**Location:**
- `src/xpst/cli.py:161-215` — `watch` command
- `src/xpst/scheduler.py:51-92` — `Scheduler.run()`

Both implement identical sleep/wake catch-up detection and loop logic. `scheduler.py` is never used by CLI (CLI has its own watch loop).

#### 10. LAYER VIOLATION: utils.video → config
**Location:** `src/xpst/utils/video.py:22`
```python
from xpst.config import EncodingConfig
```
Utils should not depend on core config. `EncodingConfig` should either live in utils or be passed as a parameter.

#### 11. NO DEPENDENCY INJECTION
**Location:** `src/xpst/engine.py:120-178`

Engine creates ALL 10+ dependencies in `__init__`. No way to inject mocks for testing. The `__init__` signature is `def __init__(self, config: XPSTConfig)` — should accept pre-built dependencies.

#### 12. PRIVATE ATTRIBUTE INJECTION
**Location:** `src/xpst/engine.py:177-178`
```python
for platform in self._platforms.values():
    platform._session_manager = self.session_manager
```
Sets a private attribute from outside the class — a code smell indicating missing proper injection mechanism.

### 🟢 LOW SEVERITY

#### 13. LAYER DETECTION ISSUE
`config.py` and `state.py` are at the core level (`src/xpst/`) but are more like infrastructure (state management, configuration loading). They could live in an `infra/` package for clearer boundaries.

#### 14. `dashboard.app` (1,310 lines, cc=143) — Largest Module
While it's a UI module (UI code tends to be verbose), 1,310 lines and complexity 143 suggests it could be split into page-specific modules.

---

## 4. Coupling Analysis

### Highest Coupling (fan-in + fan-out)
| Module | In | Out | Total | Status |
|--------|-----|------|-------|--------|
| engine | 2 | 19 | **21** | ⚠️ CRITICAL |
| utils.logger | 24 | 0 | **24** | ⚠️ (expected) |
| config | 16 | 0 | **16** | ⚠️ HIGH |
| cli | 0 | 12 | **12** | ⚠️ HIGH |

### Instability Index (out / (in+out))
- 0.0 = maximally stable, 1.0 = maximally unstable
- **engine: 0.90** — extremely unstable, depends on everything, few depend on it
- **cli: 1.00** — pure consumer (correct for entry point)
- **config: 0.00** — maximally stable (correct for config)
- **utils.logger: 0.00** — maximally stable (correct for logging)

---

## 5. Circular Dependencies
✅ **None detected.** The dependency graph is acyclic.

---

## 6. Test Coverage
- **37/39 modules tested** (95%)
- **❌ `engine.py` — NO TESTS** (1,045 lines, most complex module)
- **❌ `scheduler.py` — NO TESTS** (179 lines)
- **All 13 utils modules are tested**
- **All 5 platform modules are tested**
- **All 7 source modules are tested**

---

## 7. External Dependency Map
| Package | Used By (count) | Purpose |
|---------|-----------------|---------|
| `pathlib` | 27 modules | File paths |
| `json` | 17 modules | Serialization |
| `dataclasses` | 12 modules | Data structures |
| `subprocess` | 8 modules | FFmpeg, yt-dlp execution |
| `rich` | 7 modules | CLI formatting |
| `instagrapi` | 5 modules | Instagram API |
| `googleapiclient` | 4 modules | YouTube API |
| `twikit` | 4 modules | X/Twitter API |
| `click` | 1 module | CLI framework |
| `nicegui` | 2 modules | Dashboard framework |
| `plotly` | 1 module | Dashboard charts |
| `keyring` | 1 module | Credential storage |
| `structlog` | 1 module | Structured logging |
| `yaml` | 2 modules | Config parsing |

---

## 8. Comparison with Architecture Patterns

### vs Clean Architecture (Uncle Bob)
| Clean Arch Layer | Required | XPST Status |
|------------------|----------|-------------|
| Entities | Core business objects | ❌ Missing — no domain model |
| Use Cases | Application-specific rules | ❌ Missing — engine does everything |
| Interface Adapters | Controllers, gateways | ⚠️ Partial — base.py ABCs |
| Frameworks | External tools | ✅ Utils layer |

**Gap:** No use-case layer. Engine is simultaneously use case, orchestrator, and service.

### vs Hexagonal Architecture
- ✅ Ports defined (`platforms/base.py` ABC, `sources/base.py` ABC)
- ✅ Adapters implemented (youtube, x, instagram, tiktok)
- ❌ No primary/secondary port distinction
- ❌ Engine acts as both port adapter AND use case
- ❌ Ports too narrow — missing notification port, state port

### vs Onion Architecture
- ✅ Has concentric layers (utils → platforms → engine → cli)
- ❌ Engine depends on too many concrete implementations, not abstractions
- ❌ No domain model layer at the center

---

## 9. Recommendations (Priority Order)

### 🔴 Critical (Do First)

1. **BREAK UP `engine.py`** into use-case classes:
   ```
   xpst/usecases/
       fetch_videos.py    → FetchNewVideosUseCase
       cross_post.py      → CrossPostVideoUseCase
       health_check.py    → CheckHealthUseCase
       backfill.py        → BackfillUseCase
   ```
   Each use case receives dependencies via constructor.

2. **ADD TESTS FOR `engine.py`** — the most complex module has zero tests.

3. **FIX DUPLICATE `@dataclass`** — `engine.py:46-47`

### 🟠 High Priority

4. **EXTRACT `_upload_to_platform()`** — deduplicate the 3x upload pipeline in engine.

5. **REMOVE `is_circuit_breaker_open()` from `state.py`** — move to `utils/circuit_breaker.py`.

6. **INTRODUCE DEPENDENCY INJECTION** — make `CrossPostEngine.__init__` accept pre-built dependencies:
   ```python
   def __init__(self, config, state=None, video_processor=None, ...):
       self.state = state or StateManager(config.config_dir)
   ```

7. **DELETE `scheduler.py`** — it duplicates CLI's watch command and is never used.

### 🟡 Medium Priority

8. **DEDUPLICATE `NotificationConfig`** — pick one definition, reference from the other.

9. **FIX LAYER VIOLATION** — `utils/video.py` imports from `config`. Pass `EncodingConfig` as parameter instead.

10. **REPLACE `config.py` manual merging** with pydantic-settings or dynaconf.

### 🟢 Low Priority

11. Split `dashboard/app.py` (1,310 lines) into page-specific modules.

12. Add `import-linter` to CI to enforce layer boundaries.

13. Consider `typing.Protocol` for platform/source abstractions (more flexible than ABC).

---

## 10. Data Flow Diagram

```
User Request (CLI / Dashboard)
    │
    ▼
CrossPostEngine.check_and_post()
    │
    ├──► TikTokSource.list_videos()    ──► yt-dlp subprocess
    ├──► TikTokSource.download()        ──► yt-dlp subprocess → ~/.xpst/downloads/
    │
    ├──► [for each platform]:
    │    ├── CircuitBreakerManager.allow_request()
    │    ├── StateManager.is_video_posted()
    │    ├── QuotaManager.can_upload()
    │    ├── VideoProcessor.encode_for_platform() ──► FFmpeg subprocess
    │    ├── retry_operation(uploader.upload)      ──► Platform API
    │    ├── StateManager.mark_video_posted()
    │    └── WebhookNotifier.notify_*()
    │
    ├──► StateManager.save()            ──► ~/.xpst/state.json (atomic)
    └──► return [CrossPostResult]
```

---

## 11. Files Created
- `audit_analysis.py` — v1 analysis script
- `audit_analysis_v2.py` — v2 analysis script (fixed dependency resolution)
- This report

## 12. Issues Encountered
- Project was renamed from `CrossPSTR` to `XPST` during analysis (directory moved)
- Initial dependency resolution script had matching bugs — fixed in v2
