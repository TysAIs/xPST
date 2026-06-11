# xPST North Star

> Single source of truth for what xPST is, how it must work, where it stands today, and what to build next.
> Every factual claim cites `file:line` or a recon-lane / critic report. Where a platform makes a requirement impossible, this document says so plainly. Honesty over optimism.
>
> Status date: 2026-06-11. Repo: `~/xPST`, branch `feat/knowledge-base`, version `0.1.0` (`pyproject.toml:7`, `src/xpst/__init__.py:23`).

---

## 1. WHAT xPST IS

### One paragraph
xPST (Cross-Post Tool) is a local-first, open-source automation tool that takes a creator's short-form video from one source platform and republishes it — at full native fidelity — to every other platform they own (YouTube, Instagram, X today), tracks per-post performance across all of them in one place, and feeds the creator's own published content into a personal knowledge base that any connected AI agent can semantically query ("what did I say about X, and what performed?"). It runs three ways: a desktop GUI, a CLI, and an MCP server so AI agents can drive the entire product. It manages the unofficial third-party libraries it depends on with an update path designed never to silently break the app.

### One page
xPST solves the creator's cross-posting tax: shooting once but manually re-uploading, re-captioning, and re-checking analytics on three or four platforms. It is built around four pillars:

1. **Full-fidelity fan-out.** One source video downloads once and uploads to every connected destination. Each platform gets a correctly-encoded file (orientation-aware, modern bitrate, preserved frame rate) and a length/caption variant within that platform's limits. One platform failing never blocks the others (`engine.py:469-505`; posting-parity lane).
2. **Unified per-post analytics.** Views, likes, comments, shares (and platform-specific signals like X quotes/reposts) for every cross-posted video, in one normalized schema (`PlatformMetrics`, `analytics.py:27-63`), persisted over time so trends and "what performed well" are real, not fabricated.
3. **A personal content knowledge base.** Every video the creator publishes is transcribed (faster-whisper), distilled into cited "nuggets," embedded, and stored — so the creator (or any AI agent via MCP) can semantically search their own back catalog, weighted by what actually performed (`src/xpst/knowledge/`).
4. **Three drivable surfaces with a safe dependency story.** Desktop GUI (PySide6/QML), CLI (Click, 25 command groups), and an MCP server (13 tools). It depends on unofficial reverse-engineered clients (instagrapi, twikit) and yt-dlp, and ships an updater whose explicit job is to update those safely without bricking the app.

xPST is intended to ship cross-platform (Linux, macOS, Windows) as signed binaries plus a PyPI package, with a README and docs good enough that an enterprise evaluator downloads a signed release and it just works.

---

## 2. HOW IT SHOULD WORK — End-to-end journeys

These are the **target** journeys. Section 3 records where reality diverges.

### 2A. Human via desktop UI
1. **Install** a signed binary for their OS; Gatekeeper/SmartScreen accept it (target — see §3 release-engineering; signing not yet provisioned).
2. **Connect accounts** on the Connect page: OAuth for YouTube, session login for Instagram/X. Sessions persist (0600 files / keyring) and the app detects expiry/challenges and prompts re-auth.
3. **Post once / fan out.** Drag a video onto the window or pick a source; xPST encodes per-platform at native fidelity, previews caption + target list, and on approval uploads to all destinations. A Failures card shows any partial failures with a per-platform retry.
4. **See analytics.** The Analytics page shows real per-post metrics per platform, a six-metric matrix where the platform exposes it, week-over-week deltas from stored history, and a per-post drill-down.
5. **Knowledge base.** Published videos auto-ingest. A KB page surfaces transcription status, lets the creator semantically search nuggets, and shows which content performed.

### 2B. Human via CLI
```
xpst setup                      # guided config
xpst connect youtube            # per-platform auth
xpst run                        # one fan-out cycle from default source
xpst watch -b                   # bidirectional watch loop
xpst analytics --json           # machine-readable per-post metrics  (BROKEN today — §3)
xpst kb add <url>               # ingest content into the KB
xpst kb query "..." --json      # semantic search                    (substring today — §3)
xpst failures list / retry <id> # operator recovery loop             (does not exist — §4)
xpst update --check             # safe dependency update path
```
Every command supports `--json` for scripting; non-TTY auto-enables JSON (claimed but dead — §3 agent-surface D4).

### 2C. AI agent via MCP
A cold agent connects to the `xpst-mcp` stdio server (`pyproject.toml:105`) and:
1. Calls `xpst_providers` to discover the dynamic platform/source catalog (`server.py:645-664`).
2. Calls `xpst_auth_status` / `xpst_health` to confirm readiness.
3. Calls `xpst_run`/`xpst_post` with `dry_run` first, then for real — and gets back **per-video results with post URLs** (target; today returns only a success string — §3 agent-surface gap 7).
4. Calls `xpst_analytics` to read per-post performance (target — **no such tool exists today**, §3).
5. Calls `kb_query` to semantically search the creator's content, weighted by performance, with provenance (source URL, timestamp, score) and a result limit (target; today substring-only, unbounded, no provenance — §3).

The north-star test: *"any connected AI can mine my content weighted by what performed well"* — this is the owner's #1 requirement and is ~0% delivered at the query layer today (Critic 1 #10, Critic 3 #7).

---

## 3. CURRENT STATE VS IDEAL, PER SUBSYSTEM

Severity legend: CRIT / HIGH / MED / LOW. Effort: S (<½ day) / M (½–2 days) / L (>2 days).

### 3.1 Architecture
**Works:** Single canonical `CrossPostEngine` (`engine.py`, 1011 lines); `engine_v2.py` deleted and guarded by `tests/test_engine_consolidation.py` (19 passed). 3-layer state stack (`state_store.py` → `state_manager.py` → legacy `state.py` facade) with atomic writes, fcntl locking, corruption recovery.
**Defects:**
- HIGH `engine.py:431` — `_process_video` hardcodes `self._sources.get("tiktok")` (verified) while `check_and_post(source=...)` accepts any source and MCP forwards it (`server.py` enum includes youtube/x/instagram/local). Bidirectional path is correct (`engine.py:872` uses `post.source_platform`). Effort S.
- HIGH `state.py:188` — legacy `clear_dead_letter_queue` does `del self._state["posted_videos"][video_id]` (verified), erasing posted-history → re-post risk. Effort S.
- MED `state.py:231-233` — legacy `mark_cross_posted` writes mirror key `f"tiktok:{video_id}"` where `video_id` is already a composite key (verified) → junk keys like `tiktok:instagram:123`. Effort S.
- MED `state.py:34-41,315-334` — facade instantiates a SECOND `StateStore`; POSIX fcntl close-any-fd-drops-all-locks hazard. Effort M.
- MED `state.py:172-174,222-224` — writes `source_platform=""` (verified at :174) while backfill filters on `source_platform == source` (`engine.py:689`) → `backfill --source` never matches. Effort S.
- LOW `engine.py:466` `get_video_duration` return discarded (verified). LOW `engine.py:886` dup `download_dir`. LOW encoded-suffix list duplicated `engine.py:268,517`.
**Gaps:** legacy facade is the de-facto prod state API (all runtime imports route through the 336-line compat shim); ~600-line `usecases/` layer is dead (one test consumer); duplicate circuit breakers (in-memory used; persisted CB fields in `state_manager.py:347-374` have no prod callers → breaker resets every restart); private-attribute wiring instead of constructor injection (`engine.py:198,208-209`); no provider registry (adding a platform touches ≥5 sites).

### 3.2 Posting parity
**Works:** Two flows (unidirectional `check_and_post`, bidirectional `check_and_post_bidirectional`); per-platform upload pipeline with anti-bot/breaker/quota/encode/retry/state; one platform failing returns an `UploadResult` and doesn't block others; X maps "duplicate"→success (`x.py:158-166`).
**Defects (double-post is the cardinal sin for a cross-poster):**
- CRIT `engine.py:431` source hardcode (same as 3.1).
- HIGH `upload_service.py:249-255` + `state.py:174` — unidirectional path records no `content_hash` and `source_platform=""` → (a) source-scoped backfill empty, (b) bidirectional caption-hash dedup blind to unidirectional posts → cross-flow double-post.
- HIGH `monitor.py:119` vs raw `video_id` — incompatible state keys across flows; same video via both flows = two records, neither dedup sees the other.
- HIGH `monitor.py:163` — dedup is caption-only despite file-fingerprint util at `utils/content_hash.py:13`. Edited-caption reposts; identical-caption different videos falsely suppressed.
- HIGH `utils/retry.py:268-300` — retries re-invoke upload on timeout/connection with no server-side existence check; only X has duplicate→success → IG/YT double-post on a post-success network blip.
- HIGH `x.py:57`,`youtube.py:67` — duration limits (X 140s, YT Shorts 60s) are manifest-only; encode never trims → >140s fails on X, >180s lands as long-form YT.
- MED `state.py:233` composite-mirror corruption; `engine.py:559-578` `post_manual` does no already-posted check + `video_id=video_path.stem` collisions; `usecases/cross_post.py:41-44` calls `upload(video_id=...)` against `upload(video_path, caption)` signature → TypeError (dead layer); `upload_service.py:116-120` anti-bot deferral recorded as failure (`engine.py:947`) pollutes DLQ/health.
**Gaps + platform reality:** **TikTok as a destination does not exist** — no `platforms/tiktok.py` (verified: only base/instagram/x/youtube); no stable unofficial upload API → descope from parity claims (effort L, no clean path). No story support anywhere. No pre-flight capability matrix. Photo/text-only posts not propagated. Scheduler runs unidirectional-only (`scheduler.py:129`).

### 3.3 Video quality (the "quality not up to par" gripe — root-caused)
**Works:** Shared FFmpeg pipeline with per-platform profiles; per-platform encode; carousel stitch to 1080x1920 (proving 9:16 intent, `video.py:424`).
**Defects:**
- CRIT `video.py:213,268,323` — `scale=-2:{resolution}` treats `resolution` as HEIGHT (verified). For 1080x1920 vertical source: YT/X `resolution=1080` → 608x1080; IG `resolution=720` (verified `config.py`) → 406x720, then platforms upscale from sub-SD width. **This is the direct cause of "quality not up to par."** Fix: orientation-aware scaling targeting 1920 long edge. Effort S-M.
- HIGH IG profile obsolete: 720p/CRF23/maxrate 3500k/Main@3.0 (verified `config.py`) — Reels accept 1080x1920/High/~8-12 Mbps. Effort S.
- HIGH forced `-r 30` on all profiles halves 60fps sources (`video.py:225,280,334`). Effort S.
- HIGH always-on re-encode; `get_video_info` (`video.py:96-121`) exists but never used to skip compliant sources → every upload eats a generation. Effort M.
- MED stale-cache (filename + >1000 bytes only, `upload_service.py:513`); carousel bypasses `_encode_for_platform`; twikit `upload_media` called without `media_category='tweet_video'` (`x.py:122-125`). LOW bufsize unit parsing breaks on "3.5M"; 300s encode timeout at preset slow; yt-dlp `best[ext=mp4]` leaves source quality on the table.
**Gaps:** orientation/aspect-aware targets, modern specs, conditional fps, smart passthrough probe, quality-delta logging, config-hash-keyed cache, UI quality controls.

### 3.4 Analytics
**Works:** Normalized `PlatformMetrics` schema; YouTube Data API, X (twikit), TikTok (yt-dlp scrape) collection paths; CLI summary + export.
**Defects:**
- CRIT `analytics.py:237,249` — calls `client.load_session()` and `client.insights.get_media_insights()` which **do not exist in instagrapi** (verified present in source; real API is `load_settings`/`insights_media`). AttributeError swallowed → IG always returns []. Tests green only because MagicMock fabricates the methods (`tests/test_analytics.py:263,307`). Effort S.
- CRIT UI renders zeros: backend sends `platforms` as health objects but QML reads `platforms[i].total_views` (`AnalyticsPage.qml:55-58`); `summary.total_views` never set; `top_posts` carry no metrics. Effort M.
- HIGH GUI-thread blocking: `_refresh_analytics` (30s) builds a fresh collector each call (defeats 15-min cache) and `asyncio.run`s live fetches on the Qt thread → freezes + per-30s API hammering → ban risk. Effort M.
- HIGH fabricated "vs last week" multipliers `[0.72,0.65,0.8,0.58]` (`AnalyticsPage.qml:314`) presented as real data. Effort S.
- MED glyph concat bug, `hasData` always-truthy, TikTok placeholder `@_` URL. LOW unused `PlatformMetrics`, dead YT Analytics v2 service.
**Gaps:** **NO persistence** (15-min in-memory cache only, `analytics.py:80-82`) → no trends, no "performed well" signal, no KB-weighting foundation. **NO MCP analytics tool.** No KB↔analytics bridge.
**Honest per-platform metric matrix:**
| Metric | YouTube | Instagram | X | TikTok |
|---|---|---|---|---|
| Views | yes | `play_count` (uncollected) | yes | yes (scrape) |
| Likes | yes | yes (after IG fix) | yes | yes |
| Comments | yes | yes (after IG fix) | replies=yes | yes |
| Shares | Analytics API v2 (scaffolded, unused) | insights, **Business acct required** | retweets (mislabeled "shares") | n/a |
| Reposts | n/a | not exposed by instagrapi | quotes available, uncollected | repost_count (scrape) |
| Story-reposts | n/a (no stories) | **IMPOSSIBLE without Meta Graph Business API** | n/a (no stories) | n/a |
> **Story-reposts are collectible on ZERO platforms via this stack.** IG shares/saves require a Business/Creator account. TikTok metrics are unauthenticated-scrape-only and may break without notice. State these plainly in README/UI.

### 3.5 Agent surface (MCP + CLI for agents)
**Works:** 13 MCP tools (9 `xpst_*` + 4 `kb_*`), good schemas with `additionalProperties:false`, graceful stubs when extras missing, dynamic `xpst_providers`. Happy path (discover→auth→dry-run→post) is genuinely drivable.
**Defects:**
- HIGH `cli.py` analytics `--json` accepts `as_json` but body uses `console.print` tables unconditionally (verified — no `if as_json` branch); AGENT_GUIDE documents nonexistent JSON output.
- HIGH MCP enums hardcoded `["youtube","x","instagram"]` / `["tiktok","youtube","x","instagram","local"]` (verified `server.py:158,190,244,296`); SDK validates input → plugin providers unreachable. Effort S.
- MED `server.py:576-590` `config_show` masks only `accounts`; dumps `monitoring.__dict__` (incl. `dashboard_password_hash`, `dashboard_username`) unmasked (verified — both tiktok branches return `**acc.__dict__}` unmasked too); CLI has a proper recursive masker (`cli.py:1396`) not reused. Effort S.
- MED dead global `--json`/non-TTY auto-detect (`ctx.obj["json"]` set, read nowhere); doc drift (kb_* tools undocumented); `xpst_delete` MCP=state-only vs CLI=platform-delete (same name, opposite blast radius). LOW dup `_result_to_dict`; `kb_query` substring not semantic.
**Gaps:** no MCP analytics/scheduling/config-mutation/auth/logs tools; `kb_course`/`kb_doctor` CLI-only; no MCP resources/prompts/outputSchema; `xpst kb` has no `--json`; `xpst_run` returns only a success string (`server.py:464-466`), no per-post results/URLs.

### 3.6 Knowledge base (the headline feature)
**Works:** faster-whisper transcription, fastembed/endpoint embeddings, JSON + LanceDB stores, strict-JSON nugget extraction, durable queue, doctor, 4 MCP tools, course assembly.
**Defects:**
- CRIT `doctor.py:252` constructs `IngestionQueue` whose `__init__` calls `_requeue_stale(persist=True)` (`queue.py:60`) → a "read-only" doctor rewrites queue.json, can corrupt an active worker's claim. `Workspace.resolve` mkdirs (`workspace.py:22`) → querying a nonexistent workspace creates it. Effort S.
- HIGH retrieval never uses embeddings: `kb_query` is `needle in n.point.lower()` substring (verified `tools.py:84-85`, `cli_kb.py:88-92`); working `store.search()` + embedder reachable only by internal router. Effort S/M.
- HIGH LanceDB store + queue are dead in prod (every surface uses `JsonKnowledgeStore`; no worker, no `kb enqueue`). Effort M.
- MED source-string (not content) hash dedup → same video via 2 URLs double-ingests; non-atomic + corruption-intolerant JSON store writes (`json_store.py:48,53,39-44`); `EndpointEmbedder` lacks `model_name` → manifest records "unknown", and nothing reads it to trigger re-embed. LOW f-string where-clause injection seam; CPU-pinned transcriber; leaked temp dirs.
**Gaps (owner's vision):** no auto-ingest of own published content (engine/analytics have zero KB refs); no analytics-weighted retrieval (Nugget has no performance fields); semantic search not exposed; only yt-dlp-downloadable URLs ingest (no article/text path); no re-embed/migration command; no workspace mgmt; no nugget delete/prune.

### 3.7 Desktop UI
**Works:** 7 pages, theme single-source-of-truth (`ThemeProvider`), Lucide icon font, ~99 Accessible annotations, drag-drop posting, crash-recovery dialog.
**Defects:**
- HIGH glyph-without-icon-font (tofu) at 4 sites: `AnalyticsPage.qml:205`, `ContentPage.qml:646`, `ConnectPage.qml:643,853` (audit knows only of 1). Effort S.
- HIGH window geometry persistence dead: `root.settings` / `Qt.application.settings` don't exist (verified `main.qml:28,40,76`), try/catch swallows → never persists. Effort S.
- MED `noSplashMode` self-referential (verified `main.qml:24`); Windows `file://` strip breaks `file:///C:/...` (`main.qml:439-440`); duplicated platform tab components; hardcoded chart colors not in theme.
- LOW residual emoji/text placeholder icons; brittle FontLoader relative path; per-completion Timer churn.
**Gaps:** no component-level micro-motion (0 Behaviors in pages); no app-open choreography; **no keyboard navigability** (53 MouseAreas, 0 Buttons — unusable without a mouse); **no Knowledge Base page**; thin per-post analytics; no shared design system; muted-text contrast ~3.0:1 below WCAG AA; i18n framework present (`i18n.py`) but zero bundled locales.

### 3.8 Integrations
**Works:** lazy/function-local imports for all fragile clients (a broken lib degrades one adapter, not startup); updater tracks yt-dlp/instagrapi/twikit + xpst, offline mode, FFmpeg detection; plugin manager with honest "not a security boundary" docs; dual MIT/Apache licensing with LGPL Qt notices.
**Defects:**
- HIGH yt-dlp format strings select pre-muxed single files (`tiktok.py:50` `best[ext=mp4]/best`; `youtube.py:42-47` `best[ext=mp4][height<=1080]`) — never `bv*+ba/b` + merge, despite FFmpeg being required. Contributes to quality gripe. Effort S.
- HIGH `update_all()` runs unconstrained `pip install --upgrade` — no pins, no smoke, no rollback (`updater.py:391-413`). Effort M.
- HIGH **no `sys.frozen` guard anywhere** (verified: grep returns nothing in updater.py/cli.py) → `sys.executable -m pip` inside a PyInstaller bundle re-invokes the bundled exe, not pip. Updater is broken-by-design in desktop builds. Effort S (guard).
- MED yt-dlp binary/pip split; stale licensing docs list `authlib` which isn't a dependency; KB extras unbounded above (`fastembed>=0.3`, `lancedb>=0.5`). LOW KB `_download` no error handling/cleanup; `_version_is_newer` fallback treats downgrade as newer.
**Gaps:** safe-update architecture (constrained upgrade + post-update doctor smoke + rollback; signed remote channel; frozen-app updater candidate **tufup**, MIT); CI-enforced licensing/SBOM; upper-bound pins + scheduled adapter contract matrix.

### 3.9 Release engineering
**Defects:**
- CRIT CI has never executed: all recent GitHub Actions runs fail in 2-5s on billing ("recent account payments have failed"). The 3-OS × Py3.10-3.13 matrix, PyInstaller builds, signing, notarization have zero execution evidence; Linux is the only dev box.
- HIGH duplicate conflicting workflows both named "CI" (`ci.yml` + `test.yml`); `test.yml` is weaker (`mypy --ignore-missing-imports`) and triggers on main.
- HIGH `ci.yml` push triggers only `main`/`codex/**` (verified) — the active `feat/knowledge-base` branch gets CI only via PR; combined with billing failure it is unverified on any OS.
- HIGH frozen-binary self-update broken (no `sys.frozen` guard, §3.8).
- MED signing skip-on-missing-secret (Windows exits 0 without cert; macOS ad-hoc `-`); no tags/releases exist → PyPI Trusted Publishing + attestation untested. LOW preflight has no tag↔version↔CHANGELOG check.
**Gaps:** fix billing (gates everything); first supervised RC tag; procure Apple Developer ID ($99) + Windows cert; Linux packaging beyond bare onefile (AppImage/.deb); app self-update channel; KB extras excluded from desktop binaries with no documented story; consolidate workflows + branch filter.

### 3.10 Docs front door
**Defects:**
- HIGH KB invisible: zero `kb`/knowledge mentions in README/QUICKSTART/AGENT_GUIDE/MCP_TOOLS (the flagship feature, undocumented everywhere).
- HIGH MCP_TOOLS documents 9 of 13 tools (kb_* missing); README repeats "9 tools."
- HIGH `ENTERPRISE_READINESS.md` triple-stale ("866 passing" vs audit 1037; "8.9/10" vs same-day MASTER_PLAN "claim is currently false"; Windows-workstation framing vs Linux dev box).
- HIGH README claims TikTok as a destination (`README.md:546-548`, recurring in 3+ bidirectional example sites — sweep all) and 4 posting platforms — false (no tiktok uploader).
- MED stale/broken badges (hardcoded "866 passing", codecov/stars point at now-private repo; real coverage 46%); quickstart leads with unpublished `pip install xpst` + `cd ~/XPST`; "24 commands" vs CHANGELOG "22" vs actual 25; architecture diagram omits sources; CHANGELOG missing the entire KB subsystem. LOW orphan bullet, suspect twikit ack link, single LICENSE for dual license, SECURITY.md BOM.
**Gaps:** no KB/agent-mining story; no demo GIF/desktop screenshots; no README comparison table; thin agent quickstart; no platform-risk/ToS disclosure in README; no CODE_OF_CONDUCT/PR template; no single canonical readiness doc (6 coexist with conflicting numbers); ROADMAP/COMPETITIVE pre-date the KB/MCP/desktop feature set.

---

## 4. THE CRITICAL THINGS THE OWNER MISSED

Synthesized from the three adversarial critics, ranked by how badly they threaten the ship date, real accounts, or the marquee features. The owner asked for this section explicitly.

1. **"Ships this week on 3 OSes" has zero execution proof on 2 of them.** CI has never run (billing); no Mac/Windows hardware; signing secrets unprovisioned. The ship gate must be *proof of execution*, not local green. (Critics 1#1, 2#1, 3#5,#14)
2. **The updater is unsafe AND broken in the exact builds users download.** No version constraints/rollback, and no `sys.frozen` guard so `pip` upgrades are impossible in a PyInstaller binary — the owner's literal gripe, unmet. (Critics 1#2, 2#10, 3#6)
3. **Double-posting has multiple unguarded paths** (cross-flow key mismatch, retry-after-ambiguous-failure, DLQ-clear record deletion, caption-only dedup). The single most reputation-damaging failure for a cross-poster. (Critics 1#4, 2#3, 3#3)
4. **MCP is an unauthenticated loaded gun.** Any connected agent can post to real accounts, delete records, and read a password hash, with no consent tier, read-only mode, or rate cap. (Critic 2#2, 1#6)
5. **Performance-weighted KB is mathematically impossible without analytics persistence** — and there is none. The marquee feature's prerequisite was never specified. (Critics 2#5, 3#8)
6. **Cold-start: KB and analytics are empty on day one.** Nothing imports the creator's back catalog; posting doesn't feed the KB. (Critic 2#6)
7. **`~/.xpst/state.json` is an unrecoverable single point of failure** → lose/corrupt it and the next watch cycle re-posts the whole recent catalog publicly. No backup/export/restore. (Critic 2#7)
8. **Account-safety lifecycle is unmanaged** — session expiry, IG challenges, X shadow-limits have no detection/recovery; anti-bot deferrals are mislabeled as failures, masking real problems. (Critic 2#4)
9. **Failure recovery has no user surface** — DLQ is invisible, the only retry (backfill) is broken, and clearing the DLQ deletes posted history. (Critic 2#9)
10. **Single-account-per-platform** is baked into config/state/sessions/quotas — every creator with a brand + personal account is locked out; retrofitting after v1 fossilizes state keys. Decide and document now. (Critic 2#8)
11. **Several requested metrics are platform-impossible** (story-reposts everywhere; IG shares/saves need Business acct; TikTok as a destination has no API). Promising the full matrix would be dishonest. (Critic 3#10)
12. **Enterprise UI ask implies accessibility (WCAG):** zero focusable controls, sub-AA contrast, hollow i18n. (Critic 2#15)
13. **Dead/contradictory code layers** (usecases TypeError, dup analytics collectors, dup circuit breakers, state facade) are where the next regression hides. (Critic 3#13)
14. **Docs contradict themselves on readiness and miscount the product** — reviewers discount every claim after catching one. (Critics 1#14, 3#11)
15. **Supply chain: unofficial ToS-violating clients, unbounded pins, no honest ban-risk disclosure.** (Critics 1#11, 3#15)

---

## 5. GAP REGISTER

| ID | Sev | Effort | Subsystem | Fix summary | Cite |
|---|---|---|---|---|---|
| G01 | CRIT | S | posting/arch | Thread `source` into `_process_video`; use `self._sources.get(source)` | engine.py:431 |
| G02 | HIGH | S | arch | Clear DLQ errors field only; never `del posted_videos[id]` | state.py:188 |
| G03 | HIGH | S | posting | Write `source_platform` + composite content-hash uniformly across both flows | upload_service.py:249, state.py:174 |
| G04 | HIGH | S | posting | Switch dedup to file fingerprint (`utils/content_hash.py:13`) | monitor.py:163 |
| G05 | MED | S | arch | Fix/drop legacy `tiktok:` composite-mirror write | state.py:233 |
| G06 | MED | M | arch | Share one StateStore instance; audit fd lifecycle | state.py:34-41 |
| G07 | HIGH | M | posting | Pre-reupload existence/reconciliation check (close retry double-post window) | utils/retry.py:268-300 |
| G08 | HIGH | M | posting/quality | Enforce duration limits (trim/segment or skip-with-reason) | x.py:57, youtube.py:67 |
| G09 | MED | S | posting | `post_manual` already-posted check; stable id (not `path.stem`) | engine.py:559-578 |
| G10 | MED | S | posting/arch | Delete dead `usecases/` layer (TypeError-grade bug) | usecases/cross_post.py:41 |
| G11 | MED | S | posting | Distinguish DEFERRED from FAILED in UploadResult | upload_service.py:116-120 |
| G12 | CRIT | S-M | quality | Orientation-aware scaling (target 1920 long edge) | video.py:213,268,323 |
| G13 | HIGH | S | quality | Modernize IG profile to Reels 1080p/High/~8-10Mbps | config.py (IG=720) |
| G14 | HIGH | S | quality | Conditional fps `min(source,60)`, drop forced `-r 30` | video.py:225,280,334 |
| G15 | HIGH | M | quality | Smart passthrough via existing `get_video_info` probe | video.py:96-121 |
| G16 | HIGH | S | quality/integ | yt-dlp `bv*+ba/b` + `--merge-output-format mp4` | tiktok.py:50, youtube.py:43 |
| G17 | MED | S | quality | twikit `media_category='tweet_video'` + version pin | x.py:122-125 |
| G18 | CRIT | S | analytics | Fix instagrapi calls (`load_settings`/`insights_media`); de-mock tests | analytics.py:237,249 |
| G19 | CRIT | M | analytics | Wire real per-platform/per-post metrics into backend payload + QML | dashboard/analytics.py:564-574, AnalyticsPage.qml:55 |
| G20 | HIGH | M | analytics | Move collection off Qt thread; share one cached collector | backend.py:198-201 |
| G21 | HIGH | S | analytics | Delete fabricated `[0.72,0.65,0.8,0.58]` deltas; show "no history" until persistence | AnalyticsPage.qml:314 |
| G22 | HIGH | M | analytics | Persist per-(platform,post_id,timestamp) snapshots (SQLite/LanceDB) | analytics.py:80-82 |
| G23 | MED | S | analytics | Fix TikTok placeholder `@_` URL | analytics.py:322 |
| G24 | HIGH | S | agent | Implement `xpst analytics --json` body (honor `as_json`) | cli.py:875 |
| G25 | HIGH | S | agent/arch | Generate MCP enums from `xpst_providers` catalog | server.py:158,190,244,296 |
| G26 | MED | S | agent/sec | Reuse `_mask_sensitive_values` in MCP `config_show` (mask monitoring) | server.py:576-590 |
| G27 | HIGH | S | agent | New `xpst_analytics` MCP tool reusing collector | (no tool today) |
| G28 | MED | S | agent | Rich `xpst_run` per-post results + URLs | server.py:464-466 |
| G29 | MED | M | agent | MCP scheduling tools wrapping schedule_manager | (none) |
| G30 | CRIT | S | KB | Read-only queue load flag for doctor; no mkdir on read paths | queue.py:60, doctor.py:252, workspace.py:22 |
| G31 | HIGH | S-M | KB/agent | Expose semantic `kb_query` (store.search + embedder) with k/score/provenance | tools.py:84, cli_kb.py:88 |
| G32 | HIGH | M | KB | Wire LanceDB store as default-when-installed | workspace.py:34 |
| G33 | MED | S | KB | Content-byte hash dedup; atomic + corruption-tolerant JSON writes | resolve.py:11, json_store.py:48 |
| G34 | MED | S | KB | EndpointEmbedder `model_name`; `kb reembed` keyed off manifest | pipeline.py:81 |
| G35 | L | L | KB/analytics | Auto-ingest published posts; Nugget performance fields; weighted retrieval | (cross-subsystem) |
| G36 | M | M | KB | `xpst kb import --source <platform>` back-catalog seeding | (cold-start) |
| G37 | S | S | KB | `kb rm`/`kb prune`/workspace list-delete; tempdir cleanup | resolve.py:29 |
| G38 | HIGH | S | UI | Fix 4 tofu glyph sites + pytest QML-glyph lint | AnalyticsPage.qml:205, ContentPage.qml:646, ConnectPage.qml:643,853 |
| G39 | HIGH | S | UI | Geometry persistence via Qt.labs.settings / QSettings bridge | main.qml:40,76 |
| G40 | MED | S | UI | Windows `file:///C:/` URL fix; `noSplashMode` fix | main.qml:439-440,24 |
| G41 | M | M | UI | Shared design-system components + micro-motion + focusable controls (a11y) | desktop-ui G1,G3,G6 |
| G42 | L | L | UI | Knowledge Base page | desktop-ui G4 |
| G43 | HIGH | S | release | Fix Actions billing; delete test.yml; add feat/knowledge-base trigger | ci.yml:4-5, test.yml |
| G44 | HIGH | S | release/integ | Add `sys.frozen` guard to updater | updater.py:391-413 |
| G45 | HIGH | M | release/integ | Constrained upgrade + post-update doctor smoke + rollback | updater.py:391-413 |
| G46 | MED | M | release | Procure Apple Developer ID + Windows cert; wire signing secrets | release.yml:88-92 |
| G47 | MED | M | release | First supervised v0.1.0-rc tag on real CI; first GH Release + PyPI | (no tags) |
| G48 | MED | S | integ | Upper-bound pins; CI NOTICES/SBOM diff; drop stale authlib | pyproject.toml:90-91 |
| G49 | HIGH | S-M | docs | KB front-door docs; fix tool/command counts; remove TikTok-dest claim (3+ sites); capability matrix; platform-risk section | README.md, MCP_TOOLS.md |
| G50 | MED | M | docs | Consolidate to one canonical readiness doc; supersede the rest | ENTERPRISE_READINESS.md vs MASTER_PLAN |
| G51 | S | S | safety | State backup/export/import + post-restore reconciliation | (Critic 2#7) |
| G52 | M | M | safety | MCP read-only/require-confirm mode + rate cap | (Critic 2#2) |
| G53 | M | M | safety | Session-validity probe in `xpst health`; auth-failure alerts | (Critic 2#4) |
| G54 | S/L | S(doc)/L | arch | Decide + document single vs multi-account scope before state keys fossilize | config.py:147-185 |
| G55 | S/M | S/M | recovery | `xpst failures list/retry` + Failures UI card | (Critic 2#9) |

---

## 6. BUILD ORDER FOR SHIP WEEK (Thu 6/11 → Sat 6/14)

### Foundations first (Thu — land before dependents)
- **F1 — CI unblock (G43):** fix Actions billing, delete `test.yml`, add `feat/knowledge-base` to triggers. Gates ALL verification.
- **F2 — Video pipeline (G12,G13,G14,G16):** orientation-aware scaling + IG Reels profile + conditional fps + yt-dlp merge. Highest impact/effort; gates re-test of every upload path. Directly fixes the quality gripe.
- **F3 — Analytics foundation (G18,G22):** instagrapi API fix + persistence layer. Gates UI wiring, MCP tool, KB-weighting honesty.
- **F4 — Engine correctness (G01,G02,G03,G04):** source hardcode + DLQ + dedup/state-key unification. Gates all parity claims.

### Parallel lanes (Thu–Fri, independent ownership)
- **Lane A (analytics+agent):** G19, G20, G21, then G24, G27, G25, G26, G28.
- **Lane B (KB):** G30, G31, G32, G33, G34.
- **Lane C (docs):** G49, G50 — mechanical, no code dependency; regenerate MCP_TOOLS from live registry.
- **Lane D (UI):** G38, G39, G40, plus splash fade + shared AppButton micro-motion.
- **Lane E (integrations/safety):** G44, G45, G48, G10 (delete usecases), G11, G51.

### Owner-testing-gated (Fri–Sat — needs human + hardware)
- Tag `v0.1.0-rc` on real CI; verify macOS `.app` (sign if Apple ID lands) and Windows `.exe` by hand; first GitHub Release + PyPI publish (G46, G47). **macOS/Windows visual + manual checks are owner-gated** — schedule Fri/Sat.

### Honest descopes to v1.0 (do NOT attempt this week)
- TikTok as an upload destination — no stable API; **remove the claim** (no `platforms/tiktok.py`).
- Instagram story-reposts — **platform-impossible** without Meta Graph Business API; document.
- IG reposts, TikTok saves — not exposed.
- Analytics-weighted KB retrieval (G35) — needs persistence history first; ship semantic search now, roadmap weighting.
- Windows Authenticode signing — procurement latency; ship Windows unsigned with explicit SmartScreen note.
- TUF frozen-app self-update; KB desktop page (G42); full keyboard a11y (G41); AppImage/.deb; photo/text-post parity; multi-account (G54 implement); state-facade retirement.

### Platform-impossible summary (state in README)
Story-reposts: ZERO platforms via this stack. IG shares/saves: Business account required. X: no stories. YT/X: no story concept. TikTok metrics: unauthenticated scrape only, may break without notice. TikTok posting: no stable unofficial upload API.

---

## 7. ISC CANDIDATES

Atomic, binary, tool-probeable criteria extending the project ISA. Each is verifiable by a single named probe (`grep`/`rg`, `pytest -k`, `python -c`, `ffprobe`, `gh run`, file existence). Grouped by subsystem.

### Architecture / state
1. `grep -n 'self._sources.get("tiktok")' src/xpst/engine.py` returns nothing in `_process_video`.
2. `rg "self._sources.get\(source\)" src/xpst/engine.py` matches in `_process_video`.
3. `pytest -k process_video_nontiktok` passes (posting from a fake non-tiktok source downloads via that source).
4. `grep -n 'del self._state\["posted_videos"\]' src/xpst/state.py` returns nothing.
5. `pytest -k clear_dlq_preserves_record` passes.
6. `rg 'f"tiktok:\{video_id\}"' src/xpst/state.py` returns nothing.
7. `rg 'source_platform=""' src/xpst/state.py` returns nothing.
8. `pytest -k backfill_source_filter` passes.
9. `rg "class StateStore" src/xpst/state.py | wc -l` == 0 (facade reuses manager's store).
10. `test -d src/xpst/usecases && echo present || echo gone` prints "gone" (or `rg "from xpst.usecases" src/ --glob '!usecases/**'` returns nothing).
11. `pytest tests/test_engine_consolidation.py` passes (single engine, no engine_v2).
12. `grep -n "get_video_duration(video_path)$" src/xpst/engine.py` shows the return is assigned/used or the line is removed.
13. `python -c "import ast,sys; ast.parse(open('src/xpst/engine.py').read())"` exits 0.
14. `rg "record_circuit_breaker_failure|is_circuit_breaker_open" src/xpst --glob '!state.py'` has ≥1 prod caller, OR persisted-CB code removed.
15. `rg "_crash_recovery =|_session_manager =" src/xpst/engine.py` returns nothing (constructor injection).

### Posting parity
16. `pytest -k cross_flow_dedup` passes (same video via both flows = one record).
17. `rg "compute_caption_hash" src/xpst/monitor.py` is gone OR superseded by file-hash.
18. `rg "content_hash" src/xpst/utils/content_hash.py` exists and is imported by the upload path.
19. `pytest -k retry_no_double_post` passes (post-success network blip does not re-upload).
20. `pytest -k duration_limit_x` passes (>140s video is trimmed or skipped-with-reason on X).
21. `pytest -k duration_limit_youtube_shorts` passes.
22. `pytest -k manual_post_idempotent` passes.
23. `rg "video_path.stem" src/xpst/engine.py` not used as a state key.
24. `pytest -k deferred_not_failed` passes (anti-bot deferral != DLQ failure).
25. `test -f src/xpst/platforms/tiktok.py` — EXPECTED ABSENT for v1; README must not claim TikTok as destination.
26. `pytest -k one_platform_failure_isolated` passes (other platforms still post).
27. `rg "_stitch_and_upload" src/xpst/platforms/base.py` shows the temp file is unlinked.
28. `rg '\["_youtube","_instagram","_x"\]' src/xpst/engine.py | wc -l` <= 1 (suffix list deduped).
29. `pytest -k filter_new_zero_platforms_warns` passes.
30. `rg "source_platform" src/xpst/services/upload_service.py` shows it is passed through.

### Video quality
31. `rg "scale=-2:\{resolution\}" src/xpst/utils/video.py` returns nothing.
32. `rg "if\(gt\(a,1\)|long.?edge|1920" src/xpst/utils/video.py` shows orientation-aware scaling.
33. `python -c "from xpst.config import VideoConfig as V; assert V().encoding_instagram.resolution>=1080"`.
34. `python -c "from xpst.config import VideoConfig as V; assert V().encoding_instagram.profile=='high'"`.
35. `pytest -k vertical_1080x1920_preserved` passes (output width >= 1080 for 9:16 source).
36. `rg '"-r", *"30"|fps=30' src/xpst/utils/video.py` returns nothing (conditional fps only).
37. `pytest -k fps_preserved_60` passes.
38. `rg "get_video_info" src/xpst/services/upload_service.py` shows a compliance/passthrough probe.
39. `pytest -k passthrough_skips_compliant` passes.
40. `rg "media_category" src/xpst/platforms/x.py` shows `'tweet_video'`.
41. `rg "bv\*\+ba|merge-output-format" src/xpst/sources/youtube.py` matches.
42. `rg "bv\*\+ba|merge-output-format" src/xpst/sources/tiktok.py` matches.
43. `pytest -k bufsize_unit_parse` passes for "3.5M".
44. `rg "1000 bytes|> 1000" src/xpst/services/upload_service.py` replaced by integrity/config-hash cache check.
45. `pytest -k carousel_platform_conditioned` passes.

### Analytics
46. `python -c "import instagrapi,inspect; c=instagrapi.Client; assert not hasattr(c,'load_session')"` (confirms old API is gone — code must not call it).
47. `rg "load_session|insights\.get_media_insights" src/xpst/analytics.py` returns nothing.
48. `rg "load_settings|insights_media" src/xpst/analytics.py` matches.
49. `pytest -k analytics_instagram_real_api` passes WITHOUT a MagicMock fabricating the method.
50. `rg "get_media_insights" tests/test_analytics.py` returns nothing.
51. `pytest -k analytics_payload_has_metrics` passes (backend `summary.total_views` populated).
52. `rg "platforms\[i\]\.total_views" src/xpst/desktop_app/qml/pages/AnalyticsPage.qml` resolves to a key the backend actually sets.
53. `rg "\[0.72|0.65|0.8|0.58\]" src/xpst/desktop_app/qml/pages/AnalyticsPage.qml` returns nothing.
54. `python -c "from xpst.analytics import AnalyticsCollector"` and a persistence table/file exists after one collection run.
55. `pytest -k analytics_persists_snapshot` passes.
56. `rg "asyncio.run" src/xpst/dashboard/analytics.py` not called on the Qt/GUI thread (collection in a worker).
57. `rg '@_' src/xpst/analytics.py` returns nothing (TikTok username resolved).
58. `rg "PlatformMetrics" src/xpst/analytics.py` shows collectors emit it (not raw dicts) OR it is removed.
59. `pytest -k youtube_shares_collected` passes (Analytics v2 wired) OR shares documented N/A.
60. `pytest -k x_quotes_collected` passes.
61. `rg "AnalyticsCollector" src/xpst | grep -c "class AnalyticsCollector"` == 1 (collectors merged).

### Agent surface (MCP/CLI)
62. `xpst analytics --json | python -c "import sys,json; json.load(sys.stdin)"` exits 0.
63. `rg "if as_json" src/xpst/cli.py` matches in the analytics command body.
64. `rg '"enum": \["youtube", "x", "instagram"\]' src/xpst/mcp/server.py` returns nothing (enums dynamic).
65. `pytest -k mcp_enum_from_providers` passes.
66. `python -c "import json; ..."` — `xpst_config_show` MCP output contains no `dashboard_password_hash`.
67. `rg "_mask_sensitive_values" src/xpst/mcp/server.py` matches (CLI masker reused).
68. `rg "config.monitoring.__dict__" src/xpst/mcp/server.py` returns nothing (or is masked).
69. `pytest -k xpst_run_returns_per_post` passes (results include post URLs, not just a string).
70. `rg "xpst_analytics" src/xpst/mcp/server.py` matches (MCP analytics tool exists).
71. `rg "xpst_schedule_add" src/xpst/mcp/server.py` matches OR scheduling documented CLI-only.
72. `pytest tests/test_mcp_server.py` passes.
73. `rg "kb_query|kb_add" docs/MCP_TOOLS.md` matches (kb tools documented).
74. `grep -c '"name":' docs/MCP_TOOLS.md` reflects 13 tools.
75. `rg "def _result_to_dict" src/xpst/cli.py | wc -l` == 1.

### Knowledge base
76. `rg "_requeue_stale\(persist=True\)" src/xpst/knowledge/queue.py` is guarded by a read-only flag for doctor.
77. `pytest -k doctor_readonly` passes (doctor does not rewrite queue.json).
78. `pytest -k workspace_resolve_no_mkdir_on_read` passes.
79. `rg "needle in n.point.lower\(\)" src/xpst/knowledge/mcp/tools.py` returns nothing.
80. `rg "store.search\(" src/xpst/knowledge/mcp/tools.py` matches (semantic query exposed).
81. `pytest -k kb_query_semantic` passes (embedding-based, not substring).
82. `rg "k=|limit=|score" src/xpst/knowledge/mcp/tools.py` shows result limit + score on kb_query.
83. `pytest -k kb_query_provenance` passes (result carries source URL + timestamp).
84. `rg "LanceDBStore" src/xpst/knowledge/cli_kb.py` matches (LanceDB default-when-installed) OR documented JSON-only.
85. `pytest -k kb_content_hash_dedup` passes (same video via 2 URLs ingests once).
86. `rg "write_text" src/xpst/knowledge/store/json_store.py` replaced by atomic tempfile+replace.
87. `pytest -k kb_store_corruption_tolerant` passes.
88. `python -c "from xpst.knowledge.llm.embeddings import EndpointEmbedder as E; assert hasattr(E,'model_name') or 'model_name' in E.__init__.__code__.co_names"`.
89. `rg "def reembed|kb reembed" src/xpst/knowledge` matches OR roadmap-documented.
90. `rg "kb_course|kb_doctor" src/xpst/mcp` matches OR documented CLI-only.
91. `rg "import.*analytics" src/xpst/knowledge` matches (analytics↔KB bridge) OR roadmap-documented.
92. `rg "performance|metrics|score" src/xpst/knowledge/models.py` shows Nugget performance fields OR roadmap-documented.
93. `rg "kb import|import --source" src/xpst/knowledge/cli_kb.py` matches OR roadmap-documented.

### Desktop UI
94. `rg "font.family" src/xpst/desktop_app/qml/pages/AnalyticsPage.qml` covers the icon Text at the glyph site.
95. `pytest -k qml_glyph_lint` passes (no icon-font codepoint in a default-font Text).
96. `rg "modelData.icon \+ modelData.name" src/xpst/desktop_app/qml/pages/AnalyticsPage.qml` returns nothing.
97. Glyph fix verified at ContentPage.qml and ConnectPage.qml (`rg "providerIcon\(\)" ... font.family` present).
98. `rg "Qt.labs.settings|QSettings" src/xpst/desktop_app/qml/main.qml` matches (geometry persists).
99. `rg "root.settings|Qt.application.settings" src/xpst/desktop_app/qml/main.qml` returns nothing.
100. `rg 'replace\("file://", ""\)|substring\(7\)' src/xpst/desktop_app/qml/main.qml` handles `file:///C:/`.
101. `grep -c "QtQuick.Controls" src/xpst/desktop_app/qml/pages/*.qml` > 0 (real focusable Buttons) — a11y.
102. `rg "Behavior on" src/xpst/desktop_app/qml/components/*.qml` matches (micro-motion in shared components).
103. `test -f src/xpst/desktop_app/qml/pages/KnowledgePage.qml` — EXPECTED ABSENT for v1 (roadmap); not a ship blocker.

### Integrations / release
104. `rg "sys.frozen" src/xpst/updater.py` matches (frozen guard).
105. `pytest -k updater_frozen_guard` passes (no pip call when frozen).
106. `rg "pip install --upgrade" src/xpst/updater.py` is constrained (pins) and followed by a smoke/rollback.
107. `rg "authlib" NOTICES.md LICENSING_REPORT.md` returns nothing (or authlib is actually a dep).
108. `python -c "import tomllib;d=tomllib.load(open('pyproject.toml','rb'));print([x for x in d['project']['optional-dependencies']['knowledge'] if '<' not in x])"` prints `[]` (upper bounds present).
109. `gh workflow list` shows one CI workflow (test.yml deleted).
110. `rg "feat/knowledge-base" .github/workflows/ci.yml` matches (branch trigger) — or default branch updated.
111. `gh run list --branch feat/knowledge-base --limit 1` shows a non-billing, completed run.
112. `gh run list --limit 5` shows ≥1 run that did NOT fail in <10s (billing fixed).
113. `pytest` full suite passes on the 3-OS matrix (CI evidence, not local-only).
114. `gh release list` shows ≥1 release after RC tagging.
115. `python scripts/release_preflight.py` includes a tag↔version↔CHANGELOG consistency check.

### Docs front door
116. `rg -i "knowledge|kb " README.md` matches (KB documented at front door).
117. `rg -i "tiktok" README.md` does not present TikTok as a posting destination.
118. `grep -c "command" README.md` reflects the actual count (25) consistently with CHANGELOG.
119. `rg "9 tools|9 MCP" README.md docs/MCP_TOOLS.md` returns nothing (13 documented).
120. `rg "866 passing" README.md docs/ENTERPRISE_READINESS.md` returns nothing (or matches audited number).
121. `rg "pip install xpst" README.md` is gated by a published-on-PyPI note or removed.
122. README contains a per-platform capability matrix (analytics + video constraints).
123. README contains a platform-risk / ToS disclosure section.
124. One canonical readiness doc exists; others carry a "superseded by" note (`rg "superseded" docs/`).
125. `test -f .github/PULL_REQUEST_TEMPLATE.md && test -f CODE_OF_CONDUCT.md`.

### Safety / recovery (owner-missed)
126. `rg "state export|state import|backup" src/xpst/cli.py` matches (state durability).
127. `pytest -k state_backup_rotates` passes.
128. `rg "require_confirm|readonly" src/xpst/mcp/server.py` matches (agent guardrails) OR documented.
129. `rg "session.*valid|challenge" src/xpst/` shows a session-health probe in `xpst health`.
130. `rg "failures list|failures retry" src/xpst/cli.py` matches (operator recovery loop).
131. README or config docs state the single-vs-multi-account scope decision explicitly.
