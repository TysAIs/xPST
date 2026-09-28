# xPST deep review — every module (2026-09-28)

Scope: the whole repository, not just the release lane. Reviewed against
`origin/main` at `580b22f` (2026-09-28). Method:

1. Full module map measured from the tree (LOC, live test coverage from a
   complete suite run on this commit: 3,660 passed, 6 skipped, 1 xfailed,
   74% line coverage of 25,669 statements).
2. Five ASTRA review batches (independent Codex passes over engine+state,
   platforms+sources+media, CLI, MCP+plugins+utils, desktop/HTTP+docs), each
   asked: simplest shape? dead abstraction? forking duplication? rot at 2x
   scale? Every finding was then graded accept/reject by the reviewer, and
   every HIGH finding was re-checked against the code by hand before it was
   allowed into this table.
3. The pivot moat claims cross-checked line by line (section 3).

No personal information lives in this file.

## 1. Module map (measured, not remembered)

Cov = pytest line coverage measured on this exact commit.

| Module | Files | LOC | Cov | Purpose | Public API used elsewhere? |
|---|---|---|---|---|---|
| `src/xpst/` (40 root modules) | 40 | 28,987 | 71% | engine, CLI, config, state, analytics, auth, scheduling | Yes — this is the product |
| ├─ `cli.py` | 1 | 6,345 | — | 46 top-level / 69 leaf commands | sole CLI surface |
| ├─ `connect.py` | 1 | 2,060 | — | per-platform connect wizards | via CLI + dashboard |
| ├─ `engine.py` | 1 | 1,697 | — | `CrossPostEngine` orchestrator | every surface |
| ├─ `config.py` | 1 | 1,567 | — | dataclass config + migration (AGENTS.md wrongly says Pydantic — fixed below) | every surface |
| ├─ `analytics.py` | 1 | 1,463 | — | live analytics fetch | dashboard, MCP |
| ├─ `content.py` | 1 | 1,384 | — | content contracts, caption limits | preflight, uploaders |
| └─ 33 others | 33 | ~14,500 | — | setup tx, auth, scheduling, drafts… | mostly yes |
| `utils/` | 27 | 7,935 | 80% | atomic IO, credentials, guards, quota, redaction | Yes — importer audit found **zero** dead utils modules |
| `platforms/` | 9 | 7,058 | 78% | 7 uploaders + base contracts | engine/services |
| `dashboard/` | 7 | 4,553 | 85% | FastAPI+WS behind the UI | desktop shell only (internal) |
| `knowledge/` | 32 | 3,890 | 73% | KB store/ingest/LLM/organize | CLI + 5 MCP tools |
| `services/` | 7 | 3,398 | 82% | post/upload/source/preflight/recovery | engine |
| `sources/` | 7 | 2,786 | **42%** | TikTok/IG/YT/X/local downloaders | engine |
| `mcp/` | 2 | 2,778 | 70% | 40-tool stdio server | primary agent surface |
| `media/` | 7 | 1,985 | 86% | re-encode pipeline + per-platform specs | upload path |
| `src-tauri/` | 3 | 1,073 | — | Rust shell (sidecar, health, deep links) | desktop bundle |
| `plugins/` | 1 | 362 | 49% | loads `~/.xpst/plugins` | **partially dead** — see F-14 |
| `ui/` | 56 | 8,188 | — | Svelte UI | desktop bundle |
| `tests/` | 220 | 60,395 | — | 3,660 tests | — |
| `scripts/` | 42 | 11,036 | — | build/release/verify | CI |

Weakest surfaces by coverage: `sources/` 42%, `plugins/` 49%. Both are
network/disk-bound code that fails silently when a platform changes shape —
and the review found exactly that class of bug in each (F-01, F-14).

## 2. Findings

Severity = impact on correctness/safety at current usage. Fix cost S/M/L.
"Verdict" is this reviewer's accept/reject after hand-verification
(A = accepted after independent verification, a = accepted on the batch's
own evidence, R = rejected as wrong or already handled).

| # | Module | Sev | Finding (condensed) | Evidence | Cost | Fix now or park | Verdict |
|---|---|---|---|---|---|---|---|
| F-01 | `sources/tiktok.py` | **HIGH** | `_run_yt_dlp` returns `proc.returncode or 1` — a **successful** (rc=0) yt-dlp run is reported as rc=1, so every TikTok download path takes its failure branch and carousel detection always returns "not a carousel". Verified by live probe (`/bin/echo` → rc 1). Untested because the only TikTok tests mock the runner. | `sources/tiktok.py:140`; live probe 2026-09-28 | S | **fix now** | A |
| F-02 | `mcp/server.py` + AGENTS.md | **HIGH** | AGENTS.md tells agents "`xpst_delete` removes the local record" — but the handler now calls `engine.delete_post()` (platform-side delete) for every destination AND removes the local record. The documented agent contract for the most destructive tool is wrong in both directions. | `mcp/server.py:2616-2645`, `AGENTS.md:139-141` | S (doc) | **fix now** (doc-only today) | A |
| F-03 | `mcp/server.py` | **HIGH** | `XPST_MCP_ALLOW_ANY_PATH=1` disables the public-URL check as well as path confinement — an operator widening local paths silently also opens SSRF-shaped URL args. | `mcp/server.py:1175-1201` | S | fix now (next code PR) | a |
| F-04 | `mcp/server.py` | **HIGH** | `kb_add` never confines its local `source` path argument (validated as a URL, never as a path), so an agent can ingest any readable file outside the media roots. | `mcp/server.py:1121-1137`, `knowledge/ingest/resolve.py:18` | S | fix now (next code PR) | a |
| F-05 | `services/upload_service.py` | **HIGH** | Video upload path has no `facebook` encoding branch: `_encoding_config` raises `ValueError: Unknown platform: facebook`, so the declared FB video destination fails at encode time (unit tests mock this seam). | `upload_service.py:1022-1037` | S | fix now (next code PR) | A |
| F-06 | `services/upload_service.py` | MEDIUM | Duration guard calls `uploader.manifest()` but `manifest` is a **property** on the uploaders → `TypeError`, swallowed by `except Exception: return None` → the duration limit silently never applies (verified: `manifest` is a property on `YouTubeUploader`). | `upload_service.py:1111-1114`, `platforms/youtube.py:54` | S | fix now (next code PR) | A |
| F-07 | `state_manager.py` | MEDIUM | Success path (`mark_video_posted`, "legacy method") mutates state in-process with a 2s-throttled save and a **swallowed** save exception; a crash in the window after a real platform upload loses the posted record → duplicate post on next pass. Other mutators use the transactional `_store.update()` seam; the two paths fork. | `state_manager.py:701-723` vs `state_store.py:485-507` | M | fix now (next code PR) | a |
| F-08 | `state_store.py` | MEDIUM | Every mutation holds one global lock while re-hashing/rewriting the whole JSON blob; O(state size) per write. Fine at 2x accounts, painful at 100k records. | `state_store.py:87,339-371` | L | park (needs storage rethink) | a |
| F-09 | `schedule_manager.py` + `scheduling_engine.py` | MEDIUM | Due entries are claimed as `processing` with no lease/expiry; a worker killed mid-flight strands the claim forever (no restart recovery path found). | `schedule_manager.py:692-718`, `scheduling_engine.py:167` | M | park (design, not patch) | a |
| F-10 | `cli.py` | MEDIUM | `config set`/`import` write YAML directly, bypassing validation/migration/atomic write — a bad dotted set can save an invalid config that then auto-migrates wrongly on next load. | `cli.py:4405-4425` | M | fix now (next code PR) | A |
| F-11 | `cli.py` | MEDIUM | `delete` prints per-outcome lines to stdout even in `--json` mode (corrupts the JSON stream); `config validate --json` prints `valid:false` but exits 0; `schedule run --json` reports stale `pending` statuses. | `cli.py:3403-3411,4510-4519,5092` | S | fix now (next code PR) | A |
| F-12 | `mcp/server.py` | MEDIUM | `xpst_security_audit` hardcodes `dashboard_localhost`, `mcp_readonly`, `encrypted_storage` to `passed: True` — the audit tool can report a clean audit for a dashboard bound anywhere with the store in plaintext fallback. | `mcp/server.py:2056-2083` (verified) | S | fix now (next code PR) | A |
| F-13 | `mcp/server.py` | MEDIUM | `xpst_setup_start/resume/reset` write/delete transaction state but are absent from `_MUTATING_TOOLS`, so the consent guard doesn't cover them (deliberate-seeming: they touch no accounts, but the doc says "mutating tools are gated" without carving this out). | `mcp/server.py:1058,1233-1262` (verified absent) | S | fix now (either add or document) | a |
| F-14 | `plugins/__init__.py` | MEDIUM | The disk plugin loader is only consulted by CLI list/docs; the engine builds platform maps from static registries, so a plugin dropped in `~/.xpst/plugins/` can never become a working destination. Documented as a plugin **system**; today it is a plugin **inventory**. | `cli.py:5671-5744`, `engine.py:357`, `services/source_service.py:32` | M | park (decide: wire it or rename it) | a |
| F-15 | `src-tauri/src/lib.rs` | MEDIUM | OAuth callback URLs (with the one-time code) are written verbatim to a temp marker + shell log; the engine redacts them, the shell doesn't. Code is single-use+short-lived, so severity is bounded — but the zero-PII story says even transient files shouldn't carry it. | `src-tauri/src/lib.rs:178-193` | S | fix now (next code PR) | a |
| F-16 | `dashboard/server.py` | MEDIUM | `/health` returns `healthy` on an empty platform map (`all([])` is True) — the app pill can say healthy with zero platforms configured (`/api/health-status` handles this correctly; the two endpoints disagree). | `server.py:244`, compare `api.py:1104-1109` | S | fix now (next code PR) | a |
| F-17 | docs | MEDIUM | Stale doc claims confirmed by hand: `docs/api.md` documents a `UseCaseFactory` + `/analytics` + `/history` that don't exist (real routes are `/api/summary`, `/api/analytics/outcomes`, `/api/activity`); `README.md:70` says TikTok destination "pending external review" while INSTALL.md's canonical table says draft-mode live; `docs/MCP_TOOLS.md` platform subsets omit Facebook; AGENTS.md + ARCHITECTURE.md say "Pydantic settings" while `XPSTConfig` is a dataclass. | verified greps, this commit | S | **fix now** (doc-only) | A |
| F-18 | `dashboard/api.py` | MEDIUM | `/api/preflight` re-implements parsing/validation/planning that `PostService.preflight_*` already owns (two sources of truth for the same plan); router factory is 1,700 LOC of unrelated handlers. Rot risk when a 9th platform lands. | `api.py:1481-1601` vs `post_service.py:342-420` | M | park | a |
| F-19 | `platforms/base.py` | LOW | Shared validator hardcodes a 1 GB ceiling locally instead of reading `PLATFORM_SPECS[...].file_size_cap_mb`; caption-limit checks exist both in uploaders and in `content.py`'s table (defence in depth, but the numbers restate each other). | `base.py:829-846`, `content.py:74` | S | park | a |
| F-20 | `platforms/messenger.py` | LOW | Messenger inherits the video-uploader ABC but "upload" sends a text DM and returns the messenger homepage as post URL — contract-shaped object with non-contract semantics. | `messenger.py:377-397` | S | park | a |
| F-21 | `utils/audit_logger.py` | LOW | MCP audit log keeps message text/recipient IDs unsanitized (PII-adjacent for Messenger tools); redaction covers credential keys only. | `audit_logger.py:36-68` | S | park | a |
| F-22 | `sources/instagram.py` | LOW | Carousel download reports success when only one item of N landed on disk — incomplete carousel posts silently ship. | `sources/instagram.py:302-316` | M | park | a |
| F-23 | `sources/youtube.py`, `sources/x.py` | LOW | Blocking subprocess calls (120–600s timeouts) run on the event loop; long fetch freezes health/analytics while running. | `sources/youtube.py:169-194` | M | park | a |
| F-24 | `utils/retry.py` | LOW | Batch claimed raised-exception timeout paths bypass the unknown-outcome gate. Rejected as mostly theoretical: uploaders catch exceptions and return string-coded `UploadResult` failures, which the gate does see (reconcile markers are substring-matched on the string). Real residual gap: a timeout message without a marker keyword. | `retry.py:396-401`, `reconcile.py:55-83`, uploaders' blanket excepts | — | rejected, refined | R→low |
| F-25 | `dashboard/` docs | LOW | AGENTS.md calls the dashboard "FastAPI + WebSocket": there are no WebSocket routes; the UI polls over HTTP. (Left for a code-adjacent PR since it edits a drift-tested file's neighbour text.) | grep: zero `websocket` in `src/xpst/dashboard` + `ui/src` | S | park (doc) | a |

Explicitly REJECTED (kept for the record): "dead utils modules" (importer
audit found production callers for all 26 implementation modules — the
plugin loader is the only partially-dead loader); "state.py/state_schema.py
are ceremony" (they are compat re-export seams pinned by tests).

## 3. Moat claims, cross-checked

| Claim | Verdict | Evidence |
|---|---|---|
| "40-tool MCP surface" | **TRUE, machine-checked** | `len(server.TOOLS) == 40` derived live (not prose); `tests/test_mcp_docs_registry_parity.py` pins docs to the registry, and AGENTS.md's count claim is drift-tested. Registry re-derived on this commit: 40. |
| "per-platform re-encode profiles are actually distinct" | **TRUE** | All 6 `PLATFORM_SPECS` hashes distinct (differ in bitrate, size cap, duration cap, LUFS, modality sets). Caveat F-19: a couple of enforcement points restate the table locally instead of reading it. Caveat F-05/F-06: the encode *selector* has no facebook branch and the duration guard silently returns None, so the spec table is ahead of its enforcement for facebook. |
| "MCP mutation guards fail closed" | **TRUE for the 13 mutating tools; incomplete at the edges** | Path 1 traced by hand: default with no env vars → deny (`server.py:1090-1103`); READONLY blocks unconditionally; REQUIRE_CONFIRM needs per-call `confirm=true`; `handle_call_tool` runs `_guardrail_block` before any handler and is the only dispatch entry (`server.py:2740-2742`), so no handler is reachable around it. Gaps: setup-transaction tools mutate local state ungated (F-13), READONLY doesn't stop indirect writes from `xpst_analytics(live=true)` purging snapshots, and `xpst_security_audit` reports the readonly/dashboard-bind/encrypted-store checks as constants (F-12) — the audit tool currently lauds the very guards it should verify. |
| "dashboard mutations fail closed" | **TRUE** | Path 2 traced: `AuthMiddleware.dispatch` — every POST/PUT/PATCH/DELETE must pass `mutation_authorized()` or 401; public exceptions only `/oauth/callback` + `/webhook/*`; if the token store raises, `api_tokens = set()` → mutations still 401 (deny-on-error, `server.py:452-455`); route-level `Depends(require_api_token)` is a second belt on the mutating routes. |
| "zero personal data in repo/artifacts" | **TRUE for the tree** | No credentials/cookies/account IDs found in the tree; runtime data confined to `~/.xpst/`. Transient caveat F-15: the Rust shell writes OAuth callback URLs (with one-time codes) to a temp marker + log at login time. |
| "capability truth table is canonical" | **MOSTLY TRUE** | INSTALL.md's table is accurate and current (incl. TikTok draft-mode). But README.md:70, ARCHITECTURE.md, MCP_TOOLS.md subsets, and docs/api.md still contradict it (F-17). |
| "AGENTS.md is the drift-tested agent map" | **TRUE for counts/commands; STALE on behaviour** | Counts and command names are machine-pinned (`test_agents_docs_drift.py`). Its `xpst_delete` semantics claim is now wrong (F-02) and its "Pydantic settings" description doesn't match `config.py` (F-17). Drift tests cover counts, not semantics — that's the hole. |

## 4. Punch-list — top 5 highest-value fixes

1. **F-01 TikTok source rc bug** (`return proc.returncode or 1`). One-token
   fix, live-proven broken today; TikTok is the flagship source of a
   cross-poster. Add a non-mocked regression test (run `yt-dlp --version`
   through the real runner).
2. **F-02 + F-17 doc truth pass** (this PR). AGENTS.md's delete contract,
   "Pydantic" claim, README's TikTok line, api.md's dead endpoints,
   MCP_TOOLS.md platform subsets. Docs are the agent product surface here —
   a wrong contract line is a bug agents will faithfully reproduce.
3. **F-06 + F-05 upload-path silent failures**: duration guard calls a
   property as a method and swallows the TypeError (limits silently off);
   facebook video encoding raises at encode time. Small fixes, both behind
   mocked seams today.
4. **F-12 security-audit truthfulness**: make `xpst_security_audit` actually
   read the bind host, the readonly env, and the storage backend instead of
   asserting `passed: True`. An audit tool that grades itself is the moat
   claim with the least substance.
5. **F-07 state write-path fork**: make the success path go through the same
   transactional `_store.update()` seam as everything else and stop
   swallowing save errors after a real upload — duplicate posts are the one
   failure users forgive least.

Parked deliberately (each needs a design decision, not a patch): global-lock
JSON state at 100k records (F-08), schedule claim leases (F-09), plugin
loader wire-or-rename (F-14), dashboard preflight duplication (F-18).

## 5. Doc fixes landed with this review

- AGENTS.md: `xpst_delete` semantics corrected (platform delete + local
  record, with the PENDING caveat); "Pydantic settings" → dataclass +
  validation + migration.
- `docs/api.md`: dead `UseCaseFactory`/use-case-class section replaced with
  the real seam description; `/analytics` `/history` rows corrected to the
  routes that exist (`/api/summary`, `/api/analytics/outcomes`,
  `/api/activity`).
- README.md: TikTok destination line aligned to the canonical capability
  truth table (draft mode, not "review pending").
- Remaining doc items (MCP_TOOLS.md Facebook subsets, "FastAPI + WebSocket"
  phrasing) are listed in the findings table and were intentionally left
  for the code PR that touches those files' surfaces.

All doc edits verified against the code lines cited above; no behaviour
changed, no code touched.
