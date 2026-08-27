# xPST Codebase Architecture & Feature Map — EVIDENCE-BASED

**Scope:** Read-only architecture/feature map of `/Users/itxji/xPST` (git @ `589c723`, branch `main`, HEAD date 2026-08-26). MAP ONLY — no fixes.
**Method:** live CLI invocation, registry introspection at runtime, `find`/`wc`/`du`, full `pytest` run, git history forensics, source reads. Every claim below carries a file:line citation.
**Headline:** xPST is a large, well-tested monolith (37.8k src LOC, 1555 passing tests) whose **documentation layer is badly out of sync with the code** — including two entire architectures and one whole platform (LinkedIn) that no longer exist in the source tree.

---

## 1. Stack, entry points, CLI surface, module responsibilities

### 1.1 Language / framework / packaging
| Item | Value | Evidence |
|---|---|---|
| Language | Python ≥3.10 (dev venv is 3.11; mypy targets 3.12) | `pyproject.toml:11`, `pyproject.toml:153` |
| Packaging | hatchling, src-layout, name `xpst`, version **1.0.0** | `pyproject.toml:5-7,127-128` |
| License | "MIT OR Apache-2.0" | `pyproject.toml:10` |
| CLI framework | Click 8 | `pyproject.toml:33`; `src/xpst/cli.py:29` |
| Config | Pydantic v2 settings | `src/xpst/config.py`, `pyproject.toml:65` |
| Web | FastAPI + uvicorn | `pyproject.toml:58-59`; `src/xpst/dashboard/server.py` |
| Desktop | PySide6/QML | `pyproject.toml:82`; `src/xpst/desktop_app/main.py` |
| Async | asyncio throughout engine/platforms | `src/xpst/engine.py:344` |

### 1.2 Entry points (verified)
| Entry | Target | Evidence |
|---|---|---|
| Console script `xpst` | `xpst.cli:main` | `pyproject.toml:117` |
| Console script `xpst-mcp` | `xpst.mcp:cli_main` | `pyproject.toml:118` |
| `python -m xpst` | `xpst.cli:main` | `src/xpst/__main__.py:2-5` |
| `~/.local/bin/xpst` (the path in the task brief) | **NOT xPST** — shell wrapper that execs `hermes -p xpst` (Hermes profile wrapper) | `/Users/itxji/.local/bin/xpst` (78-byte shell script) |
| `~/bin/xpst` (the *real* wrapper) | `/Users/itxji/xPST/.venv/bin/python -m xpst` | `/Users/itxji/bin/xpst` (63-byte shell script) |
| PyInstaller bundle | `src/xpst/desktop_app/main.py` | `build_macos.spec:20` |

> ⚠️ The brief's stated CLI location `~/.local/bin/xpst` is actually a **Hermes profile shim**, not the xPST binary. The real CLI lives in the venv (`~/.local/bin` is only valid if it shadows, which it does not — `which xpst` → `/Users/itxji/bin/xpst`).

### 1.3 CLI surface — `xpst --help` (venv, live run) — **38 top-level commands**
```
analytics  app  auth  backfill  best-time  bio  build  config  connect  dashboard
delete  diagnostics  failures  followers  generate  health  kb  logs  mcp  messenger
plugins  post  providers  quota  readiness  run  schedule  search  security-audit  setup
state  status  suggest-caption  transcript  update  version  watch  wizard
```
Plus subcommands: `mcp start|list`, `messenger check-comments`, `state export|import|backup`, `failures list|retry`, `schedule add|list|remove|run|install`, `plugins docs|list`, `kb <9+ subcommands>` (group from `src/xpst/knowledge/cli_kb.py:41`). **Actual count 38** — but `AGENTS.md:12` claims "37 commands", `docs/ARCHITECTURE.md:47` claims "37 cmds", `GAP_ANALYSIS.md:3` claims "34 CLI commands". Git history shows counts were "fixed" at least 5× (commits `818ccc7`, `ca05693`, `e2b1014`) and are stale *again*.
Exit codes 0/1/2/3/4/10 (`src/xpst/cli.py:52-61`), `--json`/`--dry-run`/`--quiet` flags (`src/xpst/cli.py:129-134`).

**MCP surface:** 28 tools live-registered (`src/xpst/mcp/server.py:175` TOOLS list; runtime introspection returned 28; mirrored in `src/xpst/data/tool_registry.json` "total_tools": 28). **CHANGELOG/ROADMAP/GAP_ANALYSIS all still claim "23 MCP tools"** (CHANGELOG:36, ROADMAP:28, GAP_ANALYSIS:3).

### 1.4 Main modules & responsibilities (verified `head`/registry reads)
| Module | LOC | Responsibility |
|---|---|---|
| `src/xpst/cli.py` | 3999 | ALL CLI commands, agent-mode JSON, exit codes |
| `src/xpst/desktop_app/backend.py` | 2312 | PySide6↔QML `AppController` bridge (`desktop_app/backend.py:1`) |
| `src/xpst/mcp/server.py` | 1657 | MCP server, 28 tools, readonly/confirm guardrails |
| `src/xpst/connect.py` | 1517 | Streamlined account-connection wizard (all platforms) |
| `src/xpst/config.py` | 1174 | Pydantic settings, bcrypt dashboard auth, migration v1→v4 |
| `src/xpst/engine.py` | 1115 | `CrossPostEngine`: check_and_post/_process_video/post_manual/backfill/delete_post/check_health |
| `src/xpst/dashboard/analytics.py` | 903 | Dashboard AnalyticsCollector |
| `src/xpst/platforms/*` | 3592 (6 files) | Uploaders: instagram 755, x 692, messenger 617, tiktok 444, threads 425, youtube 361 |
| `src/xpst/services/upload_service.py` | 686 | Upload pipeline: circuit-breaker→anti-bot→quota→encode→upload (`upload_service.py:5`) |
| `src/xpst/services/source_service.py` | — | Source fetch delegation |
| `src/xpst/utils/*` | 5015 (17 files) | quota, notifications, credentials, sessions, video, errors, etc. |
| `src/xpst/sources/*` | 2785 (5 files) | tiktok 580, x 513, instagram 465, youtube 462, local 443 |
| `src/xpst/knowledge/*` | 3794 (32 files) | KB: ingest/transcribe (faster-whisper), llm client, embeddings, stores (sqlite-vec/lancedb), organize, course, doctor |
| `src/xpst/desktop_app/qml/**` | 11,160 | 10 QML pages + DetailPanel + components |
| `src/xpst/dashboard/server.py` | 502 | FastAPI: /health /metrics /state /bio /messenger webhooks |
| `src/xpst/wizard.py` | 498 | Polished resumable connection wizard |

---

## 2. Size and weight

### 2.1 LOC (wc -l, excludes `__pycache__`)
| Area | Files | LOC |
|---|---|---|
| `src/xpst/` (Python) | 109 | **37,825** |
| `tests/` | 97 | **23,631** |
| `scripts/` | 14 | 2,378 |
| QML (desktop) | 18 | 11,160 |
| top docs/README/CHANGELOG/etc (markdown) | 29 | 9,173 |
| **Total Python+QML** | | **~74,994** |

Largest single modules: `cli.py` 3999, `desktop_app/backend.py` 2312, `mcp/server.py` 1657, `connect.py` 1517, `config.py` 1174, `engine.py` 1115, `dashboard/analytics.py` 903, `platforms/instagram.py` 755, `platforms/x.py` 692, `upload_service.py` 686. Largest *test* files: `test_stress.py` 2118, `test_edge_cases.py` 1167, `test_hardening.py` 1130.

### 2.2 Dependencies
- **201 packages locked** in `uv.lock` (full name list extracted; largest by disk in `.venv/lib/python3.11/site-packages`):
  - **PySide6 1.2 GB**, lancedb 131 MB, pyarrow 123 MB, googleapiclient 97 MB, onnxruntime 70 MB, av 44 MB, plotly 40 MB, mypyc/mypyc .so 38 MB, yt_dlp 26 MB, nicegui 26 MB, numpy 24 MB, cryptography 22 MB, lxml 19 MB, mypy 17 MB, PIL 13 MB
- Direct deps per `pyproject.toml:31-69`: click, pyyaml, rich, yt-dlp, google-api-python-client, google-auth-oauthlib, google-auth-httplib2, twikit, instagrapi, authlib, structlog, prometheus-client, keyring, bcrypt, cryptography, fastapi, uvicorn, httpx, pydantic-settings, msgpack. Optional extras: `anti-ban` (curl_cffi), `mcp`, `desktop` (PySide6+pywebview), `windows`, `dashboard` (**nicegui+plotly**), `knowledge` (faster-whisper, fastembed, sqlite-vec, lancedb), `full`.
- **No pip** in the venv (`No module named pip`) — package management is `uv` only.
- ⚠️ `nicegui` + `plotly` (the `dashboard` extra) are effectively dead: `plotly` is referenced nowhere in src/tests/scripts; `nicegui` only inside the orphaned `src/xpst/desktop.py:113` (`_start_nicegui_server`). The shipped dashboard is pure FastAPI (`dashboard/server.py`).

### 2.3 dist/ artifacts (built 2026-08-26 11:12)
| Artifact | Size | Notes |
|---|---|---|
| `dist/xPST/` (PyInstaller onedir) | **343 MB** | binary `xPST` = 24.9 MB, Mach-O arm64; `_internal/` = 152 entries incl. whole Qt framework set (many symlinks: Qt3D*, QtCharts, QtDataVisualization…) |
| `dist/xPST.app/` | **346 MB** | signed bundle (`_CodeSignature/CodeResources`), arm64 |
| `build/build_macos/` | 81 MB | includes `xPST.pkg` = 24 MB; `base_library.zip`; `warn-build_macos.txt` = **226 missing-module warnings** (mostly benign Windows/optional, but includes lxml/brotli etc.) |
| `dist/xPST/_internal/` | — | ships `08ae81f…mypyc` compiled .so, orjson, googleapiclient, PySide6 |

`docker-entrypoint.sh` + `Dockerfile` (python:3.12-slim, non-root `xpst` user, `HOME=/home/xpst`) and `docker-compose.yml` (volume mounted at **`/root/.xpst`** — inconsistent with the Dockerfile's `/home/xpst/.xpst`, a config-path mismatch).

---

## 3. Feature inventory TODAY (each cited to source)

| Feature | Status | Where (file:line) |
|---|---|---|
| **Cross-post upload flow** | ✅ SHIPPED | `engine.py:344` `check_and_post`, `:442` `_process_video`, `:558` `post_manual`, `:659` carousel, `:716` `backfill`, `:840` bidirectional; pipeline in `services/upload_service.py` (quota/anti-bot/encode/upload at `:5`); sources `sources/tiktok.py|instagram.py|youtube.py|x.py|local.py`; CLI `run/watch/post/backfill` (`cli.py:259/342/407/496`); content-hash dedup `upload_service.py:214`; crash recovery `crash_recovery.py` |
| **Deletion / unpublish** | ✅ SHIPPED | CLI `xpst delete` (`cli.py:1341`), `engine.py:784` `delete_post`, abstract `platforms/base.py:127` `delete`; implemented per-platform: youtube.py:336, x.py:541, instagram.py:621, tiktok.py:425, threads.py:396, messenger.py:335; MCP tool `xpst_delete` (`tool_registry.json`) |
| **Analytics / insights** | ✅ SHIPPED (mixed fidelity) | `analytics.py` collector (565 LOC), `analytics_store.py` SQLite trend snapshots (G22), `dashboard/analytics.py` (903), `best_time.py` (best-time-to-post), `cli.py` `analytics`(:1128)/`followers`(:3548)/`best-time`(:3619); dashboard `/metrics /health /state` (`dashboard/server.py:141-196`); store tables `metric_snapshots` (`analytics_store.py:29-54`). **TikTok/Threads analytics are best-effort yt-dlp scrapes, frequently empty, surfaced as real `0`** (`GAP_ANALYSIS.md:100` still valid) |
| **Comments / DM handling** | ✅ SHIPPED (opt-in) | `platforms/messenger.py` — IG+FB comment auto-reply, `reply_rules` keyword matching, appsecret_proof, webhook signature verify; CLI `messenger check-comments` (`cli.py:1501-1525`); dashboard webhook GET/POST (`dashboard/server.py:389,401`); MCP `messenger_send`/`messenger_set_rules`/`xpst_messenger_check_comments`; `docs/setup-messenger.md` |
| **AI / agent features** | ✅ SHIPPED | MCP server 28 tools (`mcp/server.py:175`); `captions generated via knowledge/llm/client.py` (OpenAI-compatible `LLMClient`, `:40`) + `caption_gen.py`; KB subsystem (`knowledge/`): whisper transcription `ingest/transcribe.py`, embeddings `llm/embeddings.py`, stores `store/vector_sqlite|vector_lancedb`, CLI `kb`/`search`/`transcript`/`generate`/`suggest-caption` |
| **Plugins system** | ✅ SHIPPED (primitive) | `plugins/__init__.py` `PluginManager` (discovery from `~/.xpst/plugins/`, dependency install `:182`, mtime watch loop `:292`, **no sandboxing** — module body runs before `apply_sandbox` `:74`); CLI `plugins docs|list` (`cli.py:3190,3199`) |
| **Wizard / onboarding** | ✅ SHIPPED ×3 (overlap) | `wizard.py` (498 LOC, resumable, agent-mode, `--export-md`), `connect.py` (1517 LOC, "under 5 minutes"), `setup.py` (446 LOC first-time); CLI `setup`/`connect`/`wizard`/`auth`; desktop `OnboardingPage.qml`. **Three overlapping onboarding implementations** (see §4) |
| **Quotas** | ✅ SHIPPED | `utils/quota.py` `QuotaManager` + `QuotaExhaustedError`, preflight guardrail (`upload_service.py:33`), engine `quota_manager` (`engine.py:161`), CLI `xpst quota` (`cli.py:615`), tests `test_quota.py`/`test_quota_guardrail.py`; roadmap item "quota usage estimator" shipped as CLI only |
| **Notifications** | ✅ SHIPPED (webhook only) | `utils/notifications.py` `WebhookNotifier` (Discord/Telegram, async, failure-swallowing per docstring `:21-30`), wired in `engine.py:163-169`, `config.py` `notifications:` section; CLI `config`; tests `test_notifications.py`, `test_notification_config.py`. No in-app/gui notification surface |
| **Scheduling** | ✅ SHIPPED | `schedule_manager.py` (JSON at ~/.xpst/schedule.json), `scheduler.py` (watch loop), CLI `schedule add|list|remove|run|install` (`cli.py:2482-2704`) incl. OS-level install (`launchctl`/cron/Task Scheduler `_install_os_scheduler` `cli.py:2726`) |
| **Security** | ✅ SHIPPED | encrypted creds Fernet+scrypt `utils/credentials.py`, bcrypt dashboard auth (`config.py`), `security-audit` cmd (`cli.py:3805`), MCP readonly/confirm `mcp/server.py:604-641`, `SECURITY.md` |

Explicitly **absent**: no Pinterest/Snapchat/Bluesky (roadmap "under consideration"), no content calendar, no team/multi-account workspaces.

---

## 4. Dead / broken / orphaned / stale — and docs-vs-reality

### 4.1 REMOVED: LinkedIn platform — still claimed by 5 docs
- `src/xpst/platforms/linkedin.py` **does not exist** (only instagram/messenger/threads/tiktok/x/youtube). Deleted in commit **`f20922b`** ("enterprise OEM build-out", #36); `docs/setup-linkedin.md` deleted too.
- Runtime registry confirms: destinations = `[instagram, messenger, messengeradapter, threads, tiktok, x, youtube]` — no linkedin.
- Yet: **CHANGELOG.md:19** "Seven-platform posting: …and LinkedIn"; **ROADMAP.md:19** "Six-platform posting … LinkedIn"; **GAP_ANALYSIS.md** cites `platforms/linkedin.py:55` (line 47) and `:198` (line 24); **QUALITY_REVIEW.md:24** "platforms/linkedin.py:198", `:190`, `:275` `MAX_VIDEO_SIZE_GB`. **GAP_ANALYSIS/QUALITY_REVIEW were never updated after the platform was deleted** — their "two-tier platform" headline is obsolete for LinkedIn (Threads still exists and is the only remaining tier-2 destination).

### 4.2 REMOVED: "Engine v2" + usecases DI architecture — claimed by AGENTS.md + docs/ARCHITECTURE.md
- `src/xpst/engine_v2.py` and `src/xpst/usecases/` **do not exist** (`ls` fails). Only `engine.py` (a monolithic class delegating to `UploadService`/`SourceService`).
- `AGENTS.md:22-23` architecture table names both; `docs/ARCHITECTURE.md:6` diagram header "xPST Engine v2" and `:47` "dependency-injected use-cases" are fictional. The KISS reality is a single orchestrator class.

### 4.3 Dead code
- **`src/xpst/desktop.py` (443 LOC) is orphaned** — my import-graph scan over all of `src/`+`tests/` shows it is never imported anywhere (`__main__` is the only other unreferenced module, and it's a legitimate entry point). It is a pywebview + nicegui fallback desktop launcher (`:313` `import webview`, `:113` `_start_nicegui_server`) superseded by `xpst app` → `desktop_app/main.py` (PySide6, `cli.py:1442`). Its only dependencies (`pywebview`, `nicegui`) are therefore dead weight, and the `desktop` extra's pywebview pin exists solely for it.
- **`nicegui`/`plotly` (`dashboard` extra)**: zero runtime use (grep across src/tests/scripts → only the orphan above).
- **`tool_registry.json` metadata rot**: `handler` names (`"handle_run"`) don't match real handlers (`_handle_run`, and kb tools all share `_handle_kb_tool`) — `GAP_ANALYSIS.md:113`. `rate_limit` field advertised "30/min" etc. is **never enforced** by `mcp/server.py` (no rate limiter; only readonly/confirm guardrails) — `GAP_ANALYSIS.md:109` still valid. 4 `kb_*` tools flagged "(deprecated)" in the registry remain fully live.

### 4.4 Broken/half-finished
- **Duplicate platform registration:** `PlatformRegistry.auto_discover` (`platforms/base.py:270-293`) registers *every* `PlatformUploader` subclass by mangled class name, in addition to explicit module-level `PlatformRegistry.register(...)` calls (e.g. `messenger.py:617`). Result: `MessengerAdapter` is registered **twice** — as `messenger` and `messengeradapter` (runtime registry output above). Registry exposes **7 entries for 6 platforms**.
- **Triple redundant wizard stack:** `setup.py` (446) + `connect.py` (1517) + `wizard.py` (498) all implement overlapping platform-connection flows with different UX and different state files; `wizard.py` docstring even says the legacy one "crashed with EOFError on pipes" (`wizard.py:15-16`).
- **State triple-layer:** `state.py` (legacy shim), `state_manager.py`, `state_store.py`. GAP-14 (mark_video_posted only on legacy shim, silent save-fail swallow `state.py:189`) — still true (read at `state_manager.py`/`state.py`).
- **QUALITY_REVIEW R1 partially fixed:** "pidfile never called" is now *false* — `cli.py:278` calls `engine.acquire_pidfile()` (and **only** in `run`; `watch`/`post`/desktop still don't). The underlying cross-process lost-update concern (`state_store.py:377-394`) is unchanged but outside this map's fix scope.
- **Geometry claims:** QUALITY_REVIEW's CRITICAL QML findings (ContentPage unclosed fn `:185`, DetailPanel bare `if{}` `:279/417/533`) — spot-checked both files at those lines and they **compile-shaped OK today** (functions closed, blocks valid) → those were fixed after the review, but the review doc was never updated.
- **GAP-1 still real:** SchedulePage can't schedule Threads (only 4 hardcoded) — `SchedulePage.qml:20` (verified file still has `formPlatforms` with tiktok and no threads).
- **GAP-6/statistics gap:** `state_manager.py:388` `by_platform` seeds only 4 platforms (now `{youtube,x,instagram,tiktok,threads}` — threads added but any future platform silently drops again).

### 4.5 TODO/FIXME hygiene (actually clean)
- **Exactly 1 TODO in all of `src/`** (`platforms/x.py:39` — "Remove when twikit publishes a fixed release (2.4.0+)"). 0 FIXME/HACK/XXX.

### 4.6 Test-suite status vs documented claims
| Source | Claimed tests | Reality (this run) |
|---|---|---|
| AGENTS.md:17 / AGENTS.md:33 | 1534 | **1557 collected** |
| CHANGELOG.md:78 | 1427 passing, 3 skipped | 1555 passed, **2 skipped**, 0 failed |
| GAP_ANALYSIS.md:3 | ~1430 | same |
| git commit `cc83148` | "1534->1524, 1498->1524" | all stale |

All counts are stale; the suite has 30+ more tests than the newest doc claim.

---

## 5. Current UI surface

| Surface | Exists? | Evidence |
|---|---|---|
| CLI (Click+Rich, rich tables/spinners) | ✅ primary | `cli.py`, `rich` dep |
| TUI (textual/urwid/etc.) | ❌ none | no textual dep in uv.lock |
| **Desktop GUI** — PySide6/QML | ✅ `xpst app` | `desktop_app/main.py` (423 LOC) + `desktop_app/backend.py` (2312) + **10 QML pages** (About, Analytics, Compose, Connect, Content, Dashboard, Onboarding, Schedule, Settings + DetailPanel) + components (AnimatedNumber, LoadingSkeleton), 11,160 QML LOC; i18n skeleton `i18n.py`+`i18n/en.json` (only `backend.py` references it — real localization not shipped) |
| **Web dashboard** — FastAPI | ✅ `xpst dashboard` (port 8080) | `dashboard/server.py`: `/health /metrics /state` JSON + `/bio` + `/bio/edit` (auth) + messenger webhook endpoints; `/` serves an inline HTML page (`:196`); bind 127.0.0.1 default |
| Marketing/site pages | ✅ | root `index.html`, `privacy.html`, `terms.html`, `.well-known/security.txt` (GitHub Pages artifacts; committed in site commits) |
| NiceGUI graphical dashboard | ⚠️ vestigial | only reachable via dead `desktop.py:113` |

`AGENTS.md`'s "Dashboard (FastAPI + WebSocket)" — FastAPI yes, **WebSocket: no** (server.py has no WS route).

---

## 6. Tests — honest result of a real run

Command: `.venv/bin/python -m pytest -q --timeout=60 -p no:cacheprovider` in repo root.
**Result: `1555 passed, 2 skipped, 0 failed, 1 warning in ~92s` (ran twice; identical).**

- Skipped (both intentional, env-gated smoke tests, not failures):
  - `tests/test_knowledge_embeddings.py:109` — "set RUN_KB_SMOKE=1 to run the real fastembed smoke test"
  - `tests/test_knowledge_smoke.py:20` — "set RUN_KB_SMOKE=1 to run the real faster-whisper smoke test"
- Test inventory: **97 test files, 23,631 LOC**. Notable: `test_stress.py` (2118 LOC), `test_edge_cases.py` (1167), `test_hardening.py` (1130) — extensive hardening/stress suites; `test_import_linter_wall.py` + `test_knowledge_wall.py` enforce the import-linter contracts in `pyproject.toml:188-245`; `test_desktop_*` guard the PySide6 app; `test_live_platform_smoke.py` runs with mocked/live-optional paths (passed in this run).
- Honest caveats: conftest forces `CredentialStore` to skip keyring (`tests/conftest.py:14`); some collections `importorskip` optional extras (mcp/PySide6 — both installed here, so they ran); a few `skipif(win32)` are platform-specific. The suite passing does **not** validate the desktop app visually, live platform APIs, or the quality-review race conditions (they're out of test scope by design, per `QUALITY_REVIEW.md`).

---

## Appendix A — Docs-vs-reality scorecard (cross-check of CHANGELOG/ROADMAP/GAP/QUALITY claims)
| Claim | Document | Reality today |
|---|---|---|
| 7 platforms incl. LinkedIn | CHANGELOG:19 | 6 + messenger; **LinkedIn deleted** (f20922b) |
| 6 platforms incl. LinkedIn | ROADMAP:19 | stale (LinkedIn gone) |
| 23 MCP tools | CHANGELOG:36, ROADMAP:28, GAP:3 | **28** (`server.py:175`, registry) |
| 34/37 CLI commands | GAP:3, AGENTS:12, docs/ARCHITECTURE:47 | **38** (live `--help`) |
| 1427/1430/1524/1534 tests | CHANGELOG/QUALITY/GAP/AGENTS/commits | **1557 collected / 1555 pass / 2 skip** |
| engine_v2 + usecases/ | AGENTS:22-23, docs/ARCHITECTURE:6-47 | only `engine.py` |
| pidfile never called | QUALITY R1 | now called once (`cli.py:278`, run only) |
| linkedin.py:198 OOM read | QUALITY P1 | file gone; tiktok now streams (`tiktok.py:216` comment) |
| tool_registry rate_limit enforced | registry field | not enforced (GAP:109 valid) |
| Dashboard WebSocket | AGENTS | none present |

## Appendix B — Quick greps/facts
- `git status` clean; HEAD `589c723` "build(site): exact TikTok verification content" (2026-08-26); repo `.git` 9.4 MB (pack 7.7 MB).
- Stray `tiktokH5LJ7htLuZUf7F8WChFfCvLuMRklJEns.txt` + `0638436d9d83/` dir at repo root (TikTok webhook verification artifacts).
- `uv.lock` 1.1 MB, 201 packages; build toolchain uses `uv` (no pip).
- 226 PyInstaller missing-module warnings in current build.
