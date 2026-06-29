# xPST Architecture Review — Graphify Map Analysis

**Date:** 2026-06-28
**Source map:** `graphify-out/` (7,184 nodes · 13,234 edges · 514 communities)
**Built from commit:** `7a952675`
**Method:** Direct analysis of `graphify-out/graph.json` + targeted source verification (read-only).

> **Tooling note.** The installed `graphify` CLI (`/opt/homebrew/bin/graphify`) only exposes `query`, and its `query`/`explain`/`path_query` subcommands **crash** on this graph — a networkx `MultiGraph` bug in `serve.py:_subgraph_to_text` (`d = G.edges[u, v]` unpacks a 2-tuple as a 3-tuple). The `explain`, `path`, and `affected` subcommands described in the task do not exist in this build. All traversals below were therefore reproduced by parsing `graph.json` directly. **The graph data is sound; the query front-end is broken.** Fix or upgrade the CLI before relying on it for interactive exploration.

---

## Executive Summary

The graph confirms a clean, layered architecture: a single orchestrator (`CrossPostEngine`) sitting above a symmetric 6-platform uploader layer, fed by service objects and global config/state. **There are no true import cycles, no meaningful orphan modules, and platform wiring is complete and uniform.**

The real concerns are (1) **extreme coupling to `XPSTConfig`** (no dependency injection), (2) **a small amount of confirmed dead/duplicate code**, and (3) **several blind spots in the map itself** — most importantly a security-critical module (`credentials.py`) that graphify failed to extract at all. The "6 import cycles" in the report are all false positives.

---

## 1. Structural Concerns

### 1.1 God Nodes (over-connected hubs)

Measured by total incident edges across all relation types (`graph.json`):

| Rank | Node | In | Out | Total | File |
|------|------|----|-----|-------|------|
| 1 | `XPSTConfig` | 897 | 20 | **917** | `config.py` |
| 2 | `StateManager` | 475 | 81 | 556 | `state.py` (facade) |
| 3 | `UploadService` | 324 | 34 | 358 | `services/upload_service.py` |
| 4 | `InstagramUploader` | 278 | 46 | 324 | `platforms/instagram.py` |
| 5 | `CrossPostEngine` | 240 | 81 | 321 | `engine.py` |
| 6 | `XUploader` | 224 | 40 | 264 | `platforms/x.py` |
| 7 | `AnalyticsCollector` | 226 | 32 | 258 | `analytics.py` |
| 8 | `VideoProcessor` | 224 | 22 | 252 | `utils/video.py` |
| 9 | `AntiBotProtection` | 202 | 42 | 244 | `anti_bot.py` |
| 10 | `ProviderRole`/`Capability`/`AuthMode` | 224 | 4 | 228 | `providers.py` (enums) |

**`XPSTConfig` is the dominant structural risk.** It is read directly by **every** platform uploader, every source, the services, the dashboard, the desktop backend, the engine, and the CLI (897 incoming references). `explain XPSTConfig` shows it depends on almost nothing (20 out) but is depended on by everything — a classic global-singleton coupling pattern. There is no dependency injection: modules reach for config directly rather than receiving the slice they need. This makes config changes high-blast-radius and unit testing harder (matches the codebase's own internal note: *"NO DEPENDENCY INJECTION"*, Community 23).

The `providers.py` enums (`ProviderRole`, `ProviderCapability`, `AuthMode`, `ProviderManifest`) being god nodes is **healthy** — that is a shared vocabulary/contract layer working as intended, referenced uniformly by all 6 platforms.

### 1.2 Import Cycles — **all 6 reported cycles are false positives**

The report's "Import Cycles" section lists 6 cycles, **all of which are 1-file self-references, and none are real circular dependencies.** SCC analysis of the local file-import graph finds **zero multi-file cycles.** Breakdown of what each flagged "cycle" actually is:

| Reported "cycle" | Actual cause |
|------------------|--------------|
| `anti_bot.py`, `dashboard/analytics.py`, `schedule_manager.py` | `from datetime import …` resolved to a synthetic in-file stub node — extraction artifact |
| `dashboard/server.py` | duplicate `from fastapi import …` edge mis-attributed to the importing file |
| `mcp/server.py` | in-file `__all__` / re-export self-reference |
| `sources/__init__.py` (×5) | package `__init__` re-exporting its own submodule symbols (benign re-export) |

**No action needed on cycles.** This is a graph-extraction quirk, not an architecture defect.

### 1.3 Orphans — none of concern

Only **3 degree-0 nodes**, all non-code: `scripts/sign_windows.ps1`, `README.md` (×2). The Python codebase has **no orphan modules** — every source file participates in the graph.

---

## 2. Dead Code Findings

Methodology: a file is a dead-code candidate if its symbols receive **zero cross-file references of any relation type**, then confirmed against `grep` over `src/` and `tests/`.

### Confirmed dead code

- **`src/xpst/utils/retry_policy.py`** — **genuinely unused.** Zero importers anywhere in `src/` or `tests/` (verified). It is a duplicate retry implementation (`RetryExhaustedError`, `retryable()` decorator) that was superseded by **`src/xpst/utils/retry.py`**, which *is* live (used by `upload_service.py`, `utils/__init__.py`, and 4 test modules). **Recommend deletion** of `retry_policy.py` to remove the ambiguity of two retry modules.

### Not dead — flagged by the graph but verified live

The following showed 0 in-edges in the graph but are confirmed in use — these are **graph false-negatives**, not dead code (see §3):

- `utils/credentials.py` — 10 importers (engine, analytics, cli, youtube, mcp, connect, …)
- `utils/metrics.py` — imported by `dashboard/server.py`
- `dashboard/analytics.py` — imported by `cli.py`, `mcp/server.py`, `desktop_app/backend.py`
- `utils/audit_logger.py` — imported by `mcp/server.py`
- `config_migration.py` — imported by `config.py`
- `knowledge/mcp/tools.py` — imported by `mcp/server.py`

The runtime entrypoints (`__main__.py`, `desktop_app/main.py`, `mcp/server.py`, `dashboard/server.py`) correctly show 0 incoming refs because they are launched, not imported — expected, not dead.

---

## 3. Coverage Gaps (problems in the map itself)

These are gaps in **the graphify map**, which matter because the map is being used as an architectural source of truth.

### 3.1 🔴 `credentials.py` produced ZERO nodes — extraction failure

`src/xpst/utils/credentials.py` contributes **no nodes at all** to the graph, despite being imported by ~10 modules and exercised by a large test suite (`CredentialStore`, keyring fallback, encrypted storage). This is the **encrypted-credential-fallback security module (ADR-004)** — arguably the most security-sensitive file in the project — and it is **completely invisible** on the map. Any architecture/security review driven off this graph would silently miss it. Root-cause the extractor failure before trusting the map for security work.

### 3.2 🟠 Missing production import edges (false-negative edges)

- **`metrics.py`** (`MetricsTracker`, Prometheus, ADR-007): 27 symbol nodes but **0 incoming cross-file edges** — the `dashboard/server.py` import was not captured.
- **`dashboard/analytics.py`**: 55 nodes, but the only incoming edges are from **tests**; the production callers (`cli.py`, `mcp/server.py`, `desktop_app/backend.py`) are absent.

Effect: these modules look unused/peripheral on the map when they are wired into the live paths.

### 3.3 🟠 No symbol canonicalization → inflated node/community counts

The graph creates a **separate node per file** for the same symbol. `XPSTConfig` appears as a distinct node in **25 files**; `StateManager` in 4; `VideoMetadata`, `UploadResult`, `ProviderManifest`, `PlatformHealth` in all 6 platform files; etc. Consequences:
- The 7,184-node / 514-community total is **substantially inflated** by per-file symbol duplicates — many of the 514 communities (313 shown, 200 "thin" omitted) are fragmentation, not genuine modules.
- God-node edge counts in `GRAPH_REPORT.md` (e.g. `XPSTConfig` = 435) differ from raw incident counts (917) because the report counts a canonicalized subset; treat the absolute numbers as indicative, not exact.
- 26% of edges are `INFERRED` at **avg confidence 0.56** — low. Lean on the 74% `EXTRACTED` edges for any decision.

### 3.4 Knowledge-retrieval coverage — content is present

Because the `query` CLI is broken, retrieval quality couldn't be tested end-to-end, but the underlying concept nodes are richly present:
- **Video encoding** — 44 src nodes (`EncodingConfig`, `VideoProcessor`, ffmpeg install/cleanup).
- **TikTok posting** — 82 src nodes (uploader, source, auth, account config, metrics).
- **Analytics data flow** — 176 src nodes (`AnalyticsCollector`, `PlatformMetrics`, `AnalyticsStore`, follower snapshots).

The knowledge subsystem (`knowledge/**`) is the most heavily represented area of the graph. Coverage of the mapped topics is good; the limitation is the query engine, not the data.

---

## 4. Duplicate-Name / Confusable Nodes

- **🟠 Two `AnalyticsCollector` classes** — defined in both `analytics.py` (the live cross-platform collector, 258 edges) **and** `dashboard/analytics.py`. Same class name, two implementations. This is a genuine maintainability smell and causes the graph to conflate them. (The codebase's own QA notes also flag a bug in the dashboard analytics auto-discovery path.) **Recommend renaming** the dashboard variant (e.g. `DashboardAnalytics`) to eliminate the collision.
- **`CircuitBreakerManager`** (`upload_service.py` + `utils/circuit_breaker.py`) and similar — these are import/reference duplications of a single definition, **benign** (the per-file-node artifact from §3.3).
- **State layer is NOT fragmentation.** `state.py` / `state_manager.py` / `state_store.py` look like three competing implementations, but `state.py` is an intentional **backward-compat facade** that delegates to the `StateStore` + `StateManager` split (ADR-002). Its own comment confirms the dual-lock race (QA CRITICAL-1) was deliberately fixed: *"no second StateStore instance (prevents dual-lock race)."* The graph misattributes the split as duplication because it can't distinguish a re-export facade from a definition. **No action — but document it** so reviewers don't "fix" the facade.

---

## 5. Platform Wiring Verification ✅

All **6 destination platforms** are present and **uniformly wired**:

| Uploader | Referenced by | Conforms to base contract |
|----------|---------------|---------------------------|
| `InstagramUploader` | engine + own module | ✅ |
| `XUploader` | engine + own module | ✅ |
| `TikTokUploader` | engine + own module | ✅ |
| `LinkedInUploader` | engine + own module | ✅ |
| `ThreadsUploader` | engine + own module | ✅ |
| `YouTubeUploader` | engine + own module | ✅ |

Every uploader shares the same base contract — `PlatformUploader`, `UploadResult`, `PlatformHealth`, `ProviderManifest` all appear across all 6 platform files. The orchestration path is consistent:

```
CrossPostEngine (engine.py)
  → UploadService (upload_service.py)   [encode → anti-bot → quota → upload pipeline]
  → AntiBotProtection (anti_bot.py)     [direct edge confirmed]
  → {6 platform uploaders}
CrossPostEngine consumed by: cli.py · dashboard/server.py · desktop_app/backend.py · scheduler.py
```

**Source (download) side is intentionally asymmetric:** there are source modules for `instagram`, `tiktok`, `x`, `youtube`, and `local` — **4 social sources + local, but no LinkedIn/Threads source**. LinkedIn and Threads are destination-only platforms. This is reasonable but should be **documented** so the 6-vs-4 platform asymmetry isn't mistaken for a missing-source gap.

`affected state.py` (reverse dependents): `cli.py` (28), `desktop_app/backend.py` (18), `engine.py` (18), `desktop_app/models.py` (14), `source_service.py` (14), `monitor.py` (12), `diagnostics.py` (10). Changing the state contract has a wide but well-defined blast radius.

---

## 6. Recommendations

**Code**
1. **Delete `src/xpst/utils/retry_policy.py`** — confirmed dead; consolidate on `utils/retry.py`.
2. **Rename the dashboard `AnalyticsCollector`** (e.g. `DashboardAnalytics`) to remove the class-name collision with the core collector.
3. **Reduce `XPSTConfig` coupling.** 897 direct dependents make it the project's single largest fragility point. Introduce dependency injection or pass narrow config sub-objects (e.g. `EncodingConfig`, `NotificationConfig`, which already exist) instead of the whole config into platforms/services.
4. **Document, don't "fix"** the `state.py` facade (ADR-002) and the destination-vs-source platform asymmetry, so future reviewers don't mistake them for defects.

**Graph / tooling** (do before trusting this map again)
5. **Fix the broken `graphify` CLI** (`serve.py:_subgraph_to_text` MultiGraph edge unpacking) — `query`/`explain`/`path` currently crash, so the map is only usable via raw `graph.json` parsing.
6. **Root-cause why `credentials.py` extracted to zero nodes** and why `metrics.py` / `dashboard/analytics.py` lost their production import edges — a map that silently omits the security-credential module is unsafe for security review.
7. **Regenerate after upgrading**, and enable symbol canonicalization if available, to deflate the per-file duplicate nodes and get accurate node/community/god-node counts.
8. **Treat INFERRED edges (26%, conf 0.56) as hints, not facts** — decisions should rest on the 74% EXTRACTED edges.

---

## Appendix — What's Healthy

- No true import cycles.
- No orphan code modules.
- Clean orchestrator → service → platform layering with a uniform platform contract.
- All 6 destination platforms fully and symmetrically wired.
- Shared contract layer (`providers.py` enums, `platforms/base.py`) used consistently.
- Rich, well-connected knowledge subsystem.
- State dual-lock race already remediated via the StateStore/StateManager split.
