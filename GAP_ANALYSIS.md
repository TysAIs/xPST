# xPST v1.0.0 — Final Gap Analysis

**Scope:** Full-codebase, read-only audit across 6 platforms (YouTube, Instagram, X/Twitter, TikTok, Threads, LinkedIn) and 23 MCP tools.
**Verdict:** No CRITICAL data-loss or crash bugs. The headline claims (6 platforms, 23 MCP tools, 34 CLI commands, ~1430 tests) are literally true. **The dominant theme is a "two-tier platform" reality: the 4 original platforms (YouTube, Instagram, X, TikTok) are wired everywhere, while the 2 newest (Threads, LinkedIn) — and sometimes TikTok — are dropped by hardcoded platform tuples scattered across the CLI, desktop backend, QML, and stats.** A canonical `PLATFORMS` constant already exists (`src/xpst/desktop_app/backend.py:168`) but is widely ignored.

---

## Root Cause (read this first)

The single most common defect is **hardcoded narrow platform tuples** instead of iterating a shared constant. The same `("youtube", "instagram", "x")` or `(..., "tiktok")` literal is copy-pasted into ~10 locations. Each is an independent place where Threads/LinkedIn silently fall out. Fixing the root cause = define one authoritative platform list and drive every loop/choice/validator from it.

| Affected literal | Locations |
|---|---|
| `("tiktok","youtube","x","instagram")` | `cli.py:869` (connect Choice), `cli.py:904` (auth) |
| `("youtube","instagram","x")` | `backend.py:719` (preview default), `backend.py:1097` (config load), `backend.py:1959` (encode validation) |
| `("youtube","instagram","x","tiktok")` | `backend.py:871` (progress), `backend.py:1993` (rate-limit validation), `state_manager.py:388` (stats), `SchedulePage.qml:20` |

---

## Priority Summary

| # | Finding | Priority | Location |
|---|---------|----------|----------|
| 1 | `xpst connect threads/linkedin` rejected by CLI | **HIGH (BUG)** | `cli.py:869` |
| 2 | SchedulePage UI cannot schedule Threads/LinkedIn | **HIGH (GAP)** | `SchedulePage.qml:20,66` |
| 3 | README env-var reference is fabricated (`__` scheme, `XPST_RATE_LIMITS__*`, `XPST_FFMPEG_PATH`) | **HIGH (BUG)** | README §128,§657 |
| 4 | Full-pipeline integration tests cover only 3 of 6 platforms | **HIGH (GAP)** | `tests/test_integration.py:122` |
| 5 | Per-platform isolation is single-layered; pre-upload code outside try can abort batch | **HIGH (GAP)** | `upload_service.py:214,240`; `engine.py:608` |
| 6 | Docker documented as "planned" but already shipped & in use | **HIGH (BUG)** | README §177; `cli.py:3178` |
| 7 | `docs/setup-linkedin.md` is an unfinished stub | **HIGH (GAP)** | `docs/setup-linkedin.md` |
| 8 | `get_statistics` drops Threads/LinkedIn from per-platform counts | **MEDIUM (BUG)** | `state_manager.py:388` |
| 9 | `xpst auth` + auth-status omit Threads/LinkedIn | MEDIUM | `cli.py:904,945` |
| 10 | `xpst health` only reports *enabled* platforms (LinkedIn vanishes) | MEDIUM | `engine.py:1086` |
| 11 | Threads/LinkedIn config YAML keys (`client_id/secret`) ignored by loader | MEDIUM | `config.py:543` |
| 12 | backend preview/config-load/validation loops drop Threads/LinkedIn | MEDIUM | `backend.py:719,1097,1959,1993` |
| 13 | TikTok/Threads/LinkedIn analytics are best-effort yt-dlp; usually empty, shown as real `0` | MEDIUM | `analytics.py:367-490`; `backend.py:529` |
| 14 | `mark_video_posted` owned by "legacy" shim; silent save-fail swallow | MEDIUM | `state.py:150-189` |
| 15 | `tool_registry.json` advertises per-tool `rate_limit` that is never enforced | MEDIUM | `tool_registry.json` vs `server.py:615` |
| 16 | CHANGELOG "six pages" + doc page labels stale vs shipped sidebar | MEDIUM | CHANGELOG; README §334 |
| 17 | Video encoding: TikTok/Threads/LinkedIn use IG profile; mapping duplicated 3× | MEDIUM | `video.py:199,266`; `upload_service.py:622` |
| Various LOW items | see below | LOW | — |

---

## AREA 1 — Platform Completeness

**Mostly PASS.** Both `PlatformRegistry` (`platforms/base.py:269`) and `SourceRegistry` (`sources/base.py:231`) use auto-discovery, so uploaders/sources self-register. Layers 1–7 (uploaders, engine registry, `connect.py` all_platforms, analytics collectors, dashboard, backend `PLATFORMS` tuple) are clean for all 6. Threads/LinkedIn having no `sources/` module is **intentional** — they are destination-only (`platforms/threads.py:58`, `platforms/linkedin.py:55` declare `roles=(ProviderRole.DESTINATION,)`).

### GAP-1 — SchedulePage cannot schedule Threads or LinkedIn — **HIGH**
`src/xpst/desktop_app/qml/pages/SchedulePage.qml:20` — `formPlatforms: ({ youtube: true, instagram: false, x: false, tiktok: false })`; push logic at `:66-69` only emits those 4. Threads & LinkedIn have full uploaders and engine support but **cannot be selected for a scheduled post in the UI**.
**Fix:** add `threads`/`linkedin` to `formPlatforms` + checkboxes; ideally render the form from a shared platform list.

### GAP-2 — `previewPost` default targets omit threads/linkedin/tiktok — **HIGH**
`src/xpst/desktop_app/backend.py:719-724` — `enabled_platforms` built from `("youtube","instagram","x")` only; `targets = requested_platforms or enabled_platforms`. Explicit selection works for all 6, but a no-arg preview silently excludes enabled Threads/LinkedIn/TikTok from preflight.
**Fix:** use the `PLATFORMS` constant (`backend.py:168`), filtered by `.enabled`.

### GAP-3/4/5 — backend helper loops hardcode narrow tuples — **MEDIUM/LOW**
- `backend.py:1097` config-load enable toggles iterate `("youtube","instagram","x")` → imported TikTok/Threads/LinkedIn enable state dropped. (MEDIUM)
- `backend.py:1959` (encoding validation) and `:1993` (rate-limit bounds) skip Threads/LinkedIn → invalid values unvalidated; `rate_limits` serialization at `:614` omits Threads/LinkedIn so their limits never reach Settings UI. (MEDIUM)
- `backend.py:871` progress-emit loop omits Threads/LinkedIn → no initial progress tick (cosmetic). (LOW)
**Fix:** drive all four from `PLATFORMS`.

### GAP-6 — ConnectPage static fallback omits Threads/LinkedIn — **LOW**
`ConnectPage.qml:51-55` `fallbackDestinations()` lists only 3. Mitigated at runtime (`:133` overrides via `controller.getProviders()`), but if that call fails Threads/LinkedIn become unconnectable. Add them to the fallback for graceful degradation.

---

## AREA 2 — Video Encoding

**PASS with caveats.** `encode_for_platform()` dispatch (`utils/video.py:260-270`) reaches a valid branch for all 6; unknown platforms `raise ValueError` (no silent crash). Uploads route through encoding at `services/upload_service.py:259` → `_encode_for_platform` → `:651`, with passthrough, compliance-skip (fidelity invariant), caching, and partial-output cleanup.

### GAP — TikTok/Threads/LinkedIn use the Instagram profile, mapping duplicated — **MEDIUM**
`video.py:266-268` maps these 3 to `_build_instagram_cmd` (1080×1920 portrait, CRF 20, GOP 72). The fallback is **explicit/commented, not a silent `else`** — acceptable for v1.0. But: (a) the IG/Reels profile is portrait-optimized and a poor fit for LinkedIn's landscape feed video; (b) the same "which profile does platform X use" 3-vs-3 logic is copy-pasted at `video.py:199`, `video.py:266`, and `upload_service.py:622` — three copies that can drift.
**Fix:** add dedicated profiles (esp. LinkedIn landscape); factor the mapping into one helper.
*Note:* `upload_carousel_to_platform` (`upload_service.py:487`) does not call `_encode_for_platform` — it relies on `stitch_carousel_to_video`'s baked-in encode. Confirm intentional. (LOW)

---

## AREA 3 — State Management

**PASS on wiring.** The compat wrapper is sound: `state.py:32-34` constructs one `NewStateManager` and reuses *its* store (`self._store = self._new_manager._store`) to avoid a dual-lock race. The production engine imports the wrapper (`engine.py:37`). `state_store.py:247-275` seeds `health.platforms` with all 6; `posted_videos`/`content_hashes` are platform-agnostic; schema v2 with v1→v2 migration. `mark_video_posted` is fully platform-agnostic (`state.py:150-170`).

### BUG — `get_statistics` drops Threads/LinkedIn — **MEDIUM**
`state_manager.py:388` — `by_platform = {"youtube":0,"x":0,"instagram":0,"tiktok":0}`. Threads/LinkedIn posts are counted in `cross_posted_count` (`:397`) but the `if platform in by_platform` guard (`:395`) silently drops them, so per-platform stats under-report them as **zero**. Confirmed by direct read.
**Fix:** seed `by_platform` from the 6 health-platform keys.

### GAP — `mark_video_posted` lives only on the "legacy" shim — **MEDIUM**
`state.py:150-189` — the method the entire upload path depends on (`upload_service.py:330`) exists only on the wrapper, reaching into new-manager internals, and its 2-second throttled save swallows failures (`except Exception: pass`, `:189`). On a crash between throttled saves, recently-posted records can be lost from disk → re-post risk (partially mitigated by content-hash idempotency at `upload_service.py:214`, but only if the hash reached disk).
**Fix:** move `mark_video_posted` onto the new `StateManager`; replace the silent save-fail with a warning log.

*Minor (LOW):* `_ensure_state_keys` entries include `last_error: None` but `_empty_state` omits it (`state_store.py:248-274`) — harmless inconsistency.

---

## AREA 4 — Analytics Pipeline

**PASS on plumbing.** `analytics.py:179-190` dispatches `_collect_*` for all 6; `_discover_post_ids` seeds all 6 (`:508-515`). `analytics_store.py:29-54` schema is platform-agnostic (generic metric columns + `extra` JSON). `backend.py:_refresh_recent_posts` (`:487-592`) iterates `posted_to.items()` and enriches every platform with no allowlist.

### GAP — fidelity is 3 real + 3 best-effort, presented identically — **MEDIUM**
`analytics.py:367-490` — TikTok/Threads/LinkedIn all fall back to generic `yt-dlp` URL scraping; the Threads/LinkedIn handlers' own docstrings admit "no stable public metrics API," so they frequently return empty. `backend.py:529-532` then defaults `views/likes/comments/shares = 0`, so the UI shows **real-looking zeros** for platforms that simply have no collector.
**Fix:** surface a distinct "no data" state vs. a genuine `0`. Update stale module docstring (`analytics.py:7` lists only 4 platforms). (LOW)

---

## AREA 5 — MCP Server

**PASS.** Exactly **23** tools registered (`mcp/server.py:170-601`) and all 23 have live dispatch branches (`:661-706`). Names match `tool_registry.json` exactly both directions (`total_tools: 23`). All 7 mutating tools (`xpst_run, xpst_post, xpst_backfill, xpst_delete, xpst_schedule_add, kb_add, kb_organize`) are audit-logged centrally via `log_tool_invocation` (`server.py:648`) on every path (block/unknown/success/exception) — more robust than per-handler decorators.

### GAP — `tool_registry.json` advertises unenforced `rate_limit` — **MEDIUM**
The registry lists per-tool `rate_limit` (e.g. `"30/min"`) but `server.py` implements **no** rate limiting — only `XPST_MCP_READONLY` / `XPST_MCP_REQUIRE_CONFIRM` guardrails (`:615-641`). A false abuse-control claim on a local surface.
**Fix:** implement enforcement keyed on those values, or remove the field.

### GAP — registry metadata inaccurate — **LOW**
`handler` field names (`"handle_run"`) don't match real functions (`_handle_run`, kb tools share `_handle_kb_tool`). `kb_*` marked "(deprecated)" in registry only, not in server descriptions.

---

## AREA 6 — Config

**PASS.** All 6 platform config dataclasses present (`config.py:173,188,197,213,228,237`), all wired into `XPSTConfig`, `DEFAULT_CONFIG`, merge/env/save/rate_limits. Instagram defaults to `auth_mode="graph_api"` (`config.py:220,56`) — correct.

### GAP — Threads/LinkedIn YAML keys ignored — **MEDIUM**
On-disk config stores `client_id`/`client_secret` for Threads/LinkedIn, but dataclasses expect `graph_access_token`/`threads_user_id` (`config.py:231`) and `access_token`/`linkedin_user_id` (`config.py:240`); `_merge_config` (`:543-558`) reads only dataclass-named keys, so the YAML values are silently dropped.
**Fix:** align sample/saved config keys with dataclass field names.

### GAP — X defaults to unofficial `cookies` path while hidden — **LOW**
X `auth_mode="cookies"` (twikit) by default (`config.py:50,204`), but `provider_mode="official"` by default (`:395`) makes `is_community_platform("x")` true (`:872`) → X disabled out of the box. Confusing default combo. Document or default X to `api_v2`.

---

## AREA 7 — CLI

`xpst --help` (34 commands), `--version` (1.0.0), `post/analytics/schedule/auth --help` all exit 0.

### BUG — `xpst connect threads/linkedin` rejected — **HIGH**
`cli.py:869` — `type=click.Choice(["tiktok","youtube","x","instagram"])`. Confirmed live: `xpst connect threads` → `Error: ... 'threads' is not one of ...`. Yet `connect.py:858-874` fully supports all 6 (`connect_threads`/`connect_linkedin` defined). The Click Choice blocks individual connection of the two newest platforms.
**Fix:** add `threads,linkedin` to the Choice (derive from a shared constant).

### GAP — `xpst auth` restricted to 4 platforms — **MEDIUM**
`cli.py:904` `valid_platforms = {"tiktok","youtube","x","instagram"}`; dispatch (`:915-922`) has no threads/linkedin branch; `_show_auth_status` (`:945`) reports only youtube/x/instagram.

### GAP — `xpst health --json` omits LinkedIn — **MEDIUM**
`engine.check_health` (`engine.py:1086`) iterates only `self._platforms`, populated only for `enabled` platforms (`:297-342`). On the test machine LinkedIn was `enabled: false`, so it vanished from health entirely (Threads appeared only because enabled). Misleads users into thinking the platform doesn't exist.
**Fix:** report all known platforms, marking disabled ones as "disabled."

### GAP — raw tracebacks pollute `--json` — **LOW**
`xpst health --json` printed full instagrapi tracebacks to stderr before the JSON. Suppress/log at the uploader level.

---

## AREA 8 — Tests

Dedicated per-platform error matrices exist for the **newest** three: `TestTikTokUploader`/`TestThreadsUploader`/`TestLinkedInUploader` in `tests/test_phase_a_platforms.py` (success / no-token / auth-expired / rate-limit / network-error / health / followers / delete).

### GAP — full-pipeline integration covers only 3 of 6 — **HIGH**
`tests/test_integration.py:122` `test_full_pipeline_tiktok_to_all` exercises real `engine.check_and_post()` but only over youtube/x/instagram (hardcoded `:156,163`); docstring says "all platforms." `TestCircuitBreaker`/`TestRateLimit`/`TestHealthCheck` likewise assert over the same 3. Grep: threads=1, linkedin=1 in `test_integration.py`. **Threads/LinkedIn are never validated through the real orchestration path.**
**Fix:** parametrize the integration target set over all 6.

### GAP — no per-platform test files; oldest 3 lack focused upload matrix — **MEDIUM**
No `test_<platform>.py` convention. youtube/x/instagram have no equivalent of the Phase-A error matrix. LinkedIn is weakest overall (3 files, 0 integration). Add a parametrized `test_platforms.py` running the same matrix across all 6.

---

## AREA 9 — Error Handling & Resilience

**PASS with one architectural risk.** Every uploader converts auth failures into a typed `UploadResult(success=False, error="<CODE>")` rather than raising (threads.py:157, linkedin.py:133, tiktok.py:161, x.py:140, instagram.py:599, youtube.py:242). Circuit breakers are applied centrally in `upload_service.py` (`:176` gate, `:339/387/413` record), keyed by arbitrary platform name → covers all 6 uniformly.

### GAP — isolation is single-layered; pre-upload code can escape — **HIGH**
The engine orchestration loops (`engine.py:597-617`, `:694-714`, `:966-995`) have **no try/except around the upload call** — isolation depends entirely on the service's broad catch at `upload_service.py:411-419`. But several pre-upload steps run **outside** that try: the dedup hash probe (`compute_content_hash`, `:214`) and duration probe (`_probe_duration`, `:240`). A raise from either escapes `upload_to_platform` and would **abort the entire platform loop**, killing remaining platforms in the batch. Verified that today's injected-exception test passes only because the exception originates inside the service's try.
**Fix (defense-in-depth):** wrap each `upload_service.upload_to_platform(...)` call in the engine loops in try/except → per-platform `UploadResult` failure + `continue`.

### GAP — auth handled via string codes, not the typed hierarchy — **MEDIUM**
`utils/errors.py` defines `ErrorCategory`/`CategorizedError` but uploaders don't use them; the engine detects auth-expiry by string-sniffing (`_is_auth_expired`, `upload_service.py:348`) — brittle. Consider routing auth failures through a real `AuthError` type.

*LOW:* circuit-breaker behavior is only tested for youtube; no breaker test for threads/linkedin.

---

## AREA 10 — Documentation Accuracy

Headline numbers verified accurate: 34 CLI commands, 23 MCP tools, 6 platforms, ~1430 tests (1427 passing + 3 skipped). The weak spots:

### BUG — Configuration Reference env-var section is fabricated — **HIGH**
README §657 documents a `__` nested env-var scheme (`XPST_ACCOUNTS_THREADS__GRAPH_ACCESS_TOKEN`, `XPST_RATE_LIMITS__YOUTUBE`). **No `__` env var exists** in `config.py`; the real scheme is flat (`XPST_THREADS_GRAPH_ACCESS_TOKEN`, config.py:745). Following the docs literally silently overrides nothing. Also: `rate_limits` has **no** env override at all (file-only, `config.py:614`), and `XPST_FFMPEG_PATH` (README §128) **does not exist**.
**Fix:** rewrite §657 to the actual flat names; drop the rate-limit env example and `XPST_FFMPEG_PATH` (or implement them).

### BUG — Docker documented as "planned" but shipped — **HIGH**
README §177 + ROADMAP say Docker is "planned… install from source," yet `Dockerfile`, `docker-compose.yml` exist and `xpst build --target` actively uses Docker images (`cli.py:3178`).
**Fix:** move Docker to "shipped."

### GAP — `docs/setup-linkedin.md` is a ~4-line stub — **HIGH**
Says "instructions are being prepared," while README §451 + CHANGELOG/ROADMAP present LinkedIn as fully shipped. Code exists; the doc is just missing. Write the guide.

### BUG — desktop page labels/count stale — **MEDIUM**
CHANGELOG says desktop has "six pages" (it's 8, confirmed in `Sidebar.qml`/`main.qml`). Shipped sidebar renames pages to Library/Accounts/Automations, but README §334 + TUTORIAL_APP.md still call them Content/Connect/Schedule.

*LOW:* `kb migrate-store` help wording mismatch; `build --target macos` maps to a Windows image (README implies native macOS).

---

## Recommended Remediation Order

1. **One-line, high-impact:** add `threads,linkedin` to `cli.py:869` Choice (Finding 1).
2. **Root-cause sweep:** replace every hardcoded platform tuple (table at top) with iteration over a single authoritative platform constant — fixes Findings 2, 8, 9, 12 and prevents recurrence.
3. **Resilience:** wrap engine-loop upload calls in try/except (Finding 5).
4. **Docs:** fix the env-var reference, Docker status, and LinkedIn setup stub (Findings 3, 6, 7).
5. **Tests:** parametrize integration tests over all 6 platforms (Finding 4) — this would have caught most of the above.

*No CRITICAL findings. No fixes applied — this is a read-only audit.*
