# xPST — Final QA Quality Review

**Scope:** Read-only quality pass focused on edge cases, race conditions, resource
leaks, and failure modes that the 1555-test suite does not exercise. No code was
modified.

**Method:** Direct reading of the state/engine/scheduler/recovery core, plus
focused sub-audits of the platform adapters, MCP server, desktop app, and the
upload/video/analytics pipeline. Every finding carries a `file:line` reference.

---

## Executive summary — the items that actually matter

| # | Severity | Finding | Location |
|---|----------|---------|----------|
| R1 | **CRITICAL** | Cross-process lost update on `state.json` — no read-before-write under the file lock, and the instance lock (`acquire_pidfile`) is **never called anywhere** | `state_store.py:377-394`, `engine.py:218-225` |
| R2 | **HIGH** | In-process state reads are unsynchronized while writers mutate the same dict → `RuntimeError: dictionary changed size during iteration` / torn reads | `state_manager.py:75-88,257`, `state.py:39,250-256` |
| Q1 | **CRITICAL** | `ContentPage.qml` has an unclosed function → the entire Library page fails to compile/load | `desktop_app/qml/pages/ContentPage.qml:185` |
| Q2 | **CRITICAL** | `DetailPanel.qml` instantiates objects inside a bare `if {}` block (invalid QML) → page fails to load | `desktop_app/qml/pages/DetailPanel.qml:279,417,533` |
| C1 | **HIGH** | Crash recovery only knows 3 of 5 platforms — tiktok/threads incomplete uploads are invisible | `crash_recovery.py:56` |
| M1 | **CRITICAL** | MCP server is fail-open: no auth, guardrails off by default; any local agent can post/delete/ingest | `mcp/server.py:604-641` |
| M2 | **CRITICAL** | Path traversal in MCP `workspace` and `video_id` args | `knowledge/workspace.py:20-27,69-81` |
| P1 | **CRITICAL** | TikTok read the entire video into RAM (`f.read()`) → OOM on large files | `platforms/tiktok.py:216` |
| U1 | **HIGH** | The 300s ffmpeg encode runs synchronously on the asyncio event loop, freezing all concurrent uploads | `services/upload_service.py:259→651`, `utils/video.py:273` |
| S1 | **HIGH** | `schedule.json` is written non-atomically and with no lock; a crash mid-write or a corrupt file silently wipes ALL scheduled posts | `schedule_manager.py:85-89,79-81` |

The recurring theme: **the persistence and concurrency primitives assume a single
process and a single thread, but the runtime (watch scheduler + manual CLI +
desktop GUI with worker threads) routinely violates both assumptions.** Tests pass
because they exercise one component at a time, in one thread, in one process.

---

## 1. Race conditions

### R1 — CRITICAL — Cross-process lost update on `state.json`
**`state_store.py:377-394` (`update`/`save`), `state_manager.py:55-56`, `engine.py:218-225`**

`StateStore.update()` and `save()` take the cross-process file lock, but they
operate on `self._state` — the in-memory snapshot loaded **once** at `__init__`
(`state_store.py:67`). They never re-read the file from disk under the lock before
applying the change. The file lock therefore serializes the *write*, not the
*read-modify-write*.

Consequence: process A (watch mode) and process B (`xpst post` or the desktop app)
each loaded state at startup. B records a posted video and writes. A later calls
`state.save()` (`engine.py:423`, `scheduler.py:80,135`) with its **stale**
in-memory state and overwrites the file — silently erasing B's posted record. The
re-posting/duplicate-post window this opens is exactly the "scheduled vs manual
collision" risk.

This is **unmitigated**: `CrossPostEngine.acquire_pidfile()` exists
(`engine.py:218`) but `grep` across the whole `src/` tree shows **zero callers** —
neither the CLI nor the desktop backend ever acquires it. Nothing prevents two
engines from running at once.

- **Repro:** Start `xpst watch` in one terminal. In another, run `xpst post …`.
  After the manual post completes, wait for the watch loop's next
  `state.update_last_check_time()` + `save()`; reload `state.json` → the manual
  post's record is gone.
- **Fix:** Inside the file-locked section of `update()`/`save()`, reload from disk
  (`self._state = self._load()`), apply the updater to the *fresh* state, then
  write. And/or actually acquire the pidfile in every entrypoint that mutates
  state.

### R2 — HIGH — Unsynchronized in-process reads during writes
**`state_manager.py:69-88,236-269,383-421`, `state.py:39,250-256`**

`StateStore.update()` mutates the live state dict while holding `_thread_lock`
(`state_store.py:383-387`). But every read path bypasses that lock:
`StateManager._state` returns `self._store.get_raw()` — the *live* reference, not a
copy (`state_manager.py:69-71`) — and `is_posted`, `is_fully_cross_posted`,
`get_dead_letter_queue`, `get_statistics`, and `backfill`'s
`self.state.state["posted_videos"].items()` (`engine.py:752`) all iterate it
without acquiring `_thread_lock`.

In the desktop app, uploads run on daemon worker threads
(`desktop_app/backend.py:832-914`) while the GUI thread reads stats/health. A read
iterating `posted_videos` concurrently with a writer's `del`/insert raises
`RuntimeError: dictionary changed size during iteration`, or returns a torn view.

Worse, `state.py:250-256` (`mark_cross_posted`) and `state.py:39` mutate the shared
dict **with no lock at all** and **no save**, relying on a later `save()`.

- **Repro:** Drive `post_manual` on a worker thread against a state with thousands
  of `posted_videos` while repeatedly calling `get_statistics()` from another
  thread → intermittent `RuntimeError`.
- **Fix:** Make all reads go through `_thread_lock` and return copies
  (`get()` already does this; the read methods should use it instead of
  `get_raw()`).

### R3 — HIGH — Engine reference swapped underneath in-flight worker threads (desktop)
**`desktop_app/backend.py:832-914,1058,1105,1515`**

Posting actions spawn a daemon thread that drives `self._engine` on a fresh event
loop. `saveSettings`/`saveOnboarding`/`markOnboardingComplete` set
`self._engine = None` on the GUI thread while a worker may be mid
`run_until_complete(self._engine.post_manual(...))`. No lock guards `self._engine`.
Saving settings during an upload yields `'NoneType' object has no attribute
'post_manual'` and silently kills the upload (the error is routed to `error.emit`,
so no hard crash, but the post dies).

- **Fix:** Guard `self._engine` with a lock and an in-flight flag; refuse to rebuild
  the engine while a post is running.

### R4 — MEDIUM — `schedule.json` has no cross-process lock and no in-flight status
**`schedule_manager.py:156-195`**

`get_due()` returns pending entries; `mark_complete()` flips status only *after* the
post finishes. Between the two there is no "processing" state and no lock. If two
schedulers run (e.g. desktop + watch), or a single poll fires again before the post
finishes, the same entry is dispatched twice → **double-post**.

- **Fix:** Add a `processing` status set atomically when claimed, plus a file lock
  on `schedule.json`.

### R5 — LOW — Stale `.state` reference in the legacy facade
**`state.py:39,98`**

`self.state = self._new_manager._state` captures the dict reference once at init.
After any `load_fresh()`/`reload()` the store swaps in a *new* dict, but the
facade's cached `.state`/`._state` still point at the old one. `backfill` reads
`self.state.state[...]` (`engine.py:752`) — it can observe pre-reload data.

---

## 2. Error recovery

### C1 — HIGH — Crash recovery is blind to 3 of 5 platforms
**`crash_recovery.py:56`**

`find_incomplete_uploads` hardcodes `all_platforms = {"youtube", "x", "instagram"}`.
The project now supports five platforms. A video posted to YouTube but missing on
Threads/TikTok is **not** reported as incomplete, so recovery never
resumes it. This directly answers "does crash_recovery recover from all failure
modes?" — no, it ignores most of the platforms.

- **Fix:** Derive `all_platforms` from the configured/enabled platform set.

### Process killed during `state.json` write — SAFE (process kill) / gap (power loss)
**`state_store.py:282-325`**

`_atomic_write` writes a temp file then `os.replace` (atomic rename). A **process
kill** (SIGKILL) is safe: either the old file is intact (rename not yet done) or the
new file is complete. **However:**

- **R6 — MEDIUM:** there is no `tmp.flush()` + `os.fsync(tmp.fileno())` before the
  rename and no directory fsync after. On **power loss / OS crash**, the rename
  metadata can land while the data blocks have not, leaving a zero-length or
  truncated `state.json`. (Backup recovery in `_load` mitigates but the freshest
  write is lost.)
- **R7 — LOW:** A kill between temp-write and rename leaves orphaned
  `state.json.tmp.*` files; nothing ever cleans them up, so repeated crashes leak
  temp files in the config dir.

### S1 — HIGH — `schedule.json` write is not atomic; corruption wipes all schedules
**`schedule_manager.py:85-89` and `79-81`**

`_save` does a plain `open(...,"w")` + `json.dump` — no temp+rename. A crash mid-write
truncates the file. `_load` then catches `JSONDecodeError` and resets `_entries` to
`[]` (`schedule_manager.py:79-81`), so a single corrupt write **silently deletes
every scheduled post** with only a `warning` log. Contrast with the careful atomic
write + backup rotation in `state_store.py`.

- **Fix:** Use the same temp-file + `os.replace` atomic write pattern (and keep a
  backup) for `schedule.json`.

### ffmpeg killed mid-encode — mostly handled, one gap
**`utils/video.py:273-288`, `services/upload_service.py:654-663`**

A non-zero ffmpeg return code is checked and the partial output is deleted
(`video.py:283-287`, with a `<1000` byte guard). A SIGKILL surfaces as a non-zero
return code, so that path is fine. **Gap (MEDIUM, U2):** on `subprocess.run(...,
timeout=300)` **timeout**, `TimeoutExpired` is raised *before* `result` is assigned,
so `video.py`'s own cleanup is skipped — the partial file is only removed by the
caller's `except` in `upload_service.py:654`. And the cached-encode reuse check
(`upload_service.py:646-648`) accepts any pre-existing `*_platform` file `>1000`
bytes without re-probing integrity, so a truncated leftover can be uploaded as a
"cached encoding" (LOW, U3).

### Platform API timeout / token expiry mid-upload — gaps
- **P2 — CRITICAL:** TikTok access tokens last 24h; `_refresh_access_token` exists
  (`tiktok.py:101-136`, Threads `threads.py:106-132`) but is **never called**. A
  401 mid-upload returns `*_AUTH_EXPIRED` and gives up permanently despite a valid
  refresh token being configured.
- **P3 — HIGH:** Partial-upload server state is never aborted. If the API times out
  after a container/asset/media session is created but before publish
  (`instagram.py:362→235`, `x.py:165-216`, `tiktok.py:175-237`), the orphaned
  resource is abandoned; retry starts a fresh
  one.
- **P4 — MEDIUM:** X thread/carousel (`x.py:449-461`) posts tweet-by-tweet; cookie
  expiry after the first tweet leaves a partially-published thread but the caller is
  told the whole post failed — no rollback.
- **U4 — MEDIUM:** `upload_service` sets no `asyncio.wait_for` around
  `uploader.upload` (`upload_service.py:314`); a hung SDK socket hangs the pipeline
  forever (a hang is not an exception, so retries never fire).

### Disk full during download/encode
**`utils/disk.py:26-62`, `services/upload_service.py:198-207`**

- **U5 — HIGH:** `check_disk_space` is a one-shot pre-flight requiring only 500 MB
  (`disk.py:16`) and **fails open** — any `OSError` in the check returns `True`
  (`disk.py:60-62`). A high-bitrate encode whose output exceeds the free space fills
  the disk mid-write despite passing the check. (The resulting ffmpeg failure is
  caught and the partial removed, so no corruption — but the check did not
  *prevent* anything.)
- **Fix:** Size the requirement to the expected output (input × headroom), re-check,
  and treat `OSError` as a hard failure for large writes.

---

## 3. Resource leaks

- **P1 — CRITICAL:** `tiktok.py:216` does
  `video_bytes = f.read()` — the whole file into RAM (base validator allows up to
  1GB, `base.py:207`). httpx then often doubles it during encoding → OOM on a
  memory-constrained host. Instagram (`instagram.py:377`) and X (`x.py:189`) stream
  correctly; TikTok should too (pass the file object as `content=`).
- **httpx.AsyncClient:** No leaks found — every instance across all five adapters
  uses `async with`. The two grep hits (`instagram.py:295,328`) are type-annotated
  function *parameters*, not constructions. ✅
- **File handles:** all `open()` calls in `src/xpst/` use `with`; the grep hits for
  bare `open(` were method names (`is_circuit_breaker_open`, `_open`) and the two
  `subprocess.Popen` sites (see below). ✅
- **M4 — LOW (analytics):** `analytics_store.py` uses `with self._connect() as
  conn:` — but `with` on a sqlite3 connection commits, it does **not close**. Each
  query opens a connection finalized only by GC. Under heavy analytics churn this is
  many connections without deterministic close. Use `contextlib.closing`.
- **H2 (desktop) — HIGH:** the MCP subprocess launched by
  `desktop_app/backend.py:1369` (`subprocess.Popen`) is never terminated on app
  exit — no `aboutToQuit`/`atexit`/`closeEvent` hook calls `stopMcpServer()`. Quit
  the app → orphaned `xpst mcp` process survives.
- **M3 (desktop) — MEDIUM:** `getThumbnail` runs `subprocess.run(ffmpeg, timeout=10)`
  synchronously on the GUI thread, looped over up to 50 files in `getLocalVideos`
  (`backend.py:1605-1646,1679-1682`) and bound directly to QML `Image.source` — UI
  freezes up to 50×10s on a cold cache.
- **M5 (analytics) — LOW:** dead `FFmpegProgressParser` (`utils/progress.py:139-227`)
  — no `Popen` ever feeds it stderr; the real encode uses buffered
  `subprocess.run`, so live encode progress is never reported.

---

## 4. Input validation gaps

- **Invalid YAML — MEDIUM (V1):** `config.py:443` uses `yaml.safe_load` (good, no
  code exec) and handles empty/non-dict (`:444-451`), but a **syntax-malformed**
  YAML raises an uncaught `yaml.YAMLError` that propagates as a raw traceback rather
  than a friendly message. (Also `if file_config is None` at `:444` is dead — the
  `or {}` on `:443` already removed `None`.)
- **Corrupt video — handled/inconsistent:** `get_video_duration`
  (`utils/progress.py:230-266`) degrades to `0.0` on a corrupt file (LOW, U6 — but
  `0.0` is an ambiguous sentinel that downstream duration-limit checks read as "no
  limit exceeded"). `get_video_info` (`video.py:180`) instead *raises* `RuntimeError`;
  `is_platform_compliant` (`video.py:200`) calls it with no try/except (MEDIUM, U7).
- **Empty / oversized caption — MEDIUM (P5):** oversized captions are truncated on
  every platform (good), but **empty** captions are unvalidated. YouTube degrades to
  a `"New Short"` title (`youtube.py:181-186`); X/TikTok/Threads send empty
  text and surface an opaque API error.
- **Empty `platform_list`:** `post_manual(platforms=None)` defaults to all enabled
  (`engine.py:584-585`); an explicit `[]` yields a `CrossPostResult` with no results
  and `all_success=False` (silent no-op — LOW).
- **Missing `download_dir`:** created on demand by callers via
  `mkdir(parents=True)`; the pre-download disk check runs against it (`engine.py:473`).
  Acceptable.
- **Platform limits not validated before upload — HIGH (P6):** none of the documented
  limits are checked client-side; the code "sends and crashes on API error."
  Declared constants are dead code:
  - YouTube Shorts ≤60s (`youtube.py:12,68`) — never checked; a 10-min file silently
    becomes a regular video.
  - Instagram Reels 90s / 250MB / 9:16 (`instagram.py:10-14`) — none enforced.
  - X 140s / 512MB (`x.py:11-14`) — none enforced.
  - Threads `MAX_VIDEO_DURATION_SECONDS`/`MAX_VIDEO_SIZE_GB` (`threads.py:42-43`) —
    defined, never referenced.
- **P7 — HIGH:** `base.py:206-209` `_validate_video` uses a **local** `max_size_gb=1`
  instead of a class attribute, so subclasses' `MAX_VIDEO_SIZE_GB` overrides have no
  effect — every platform shares a hardcoded 1GB limit regardless of its real
  ceiling.

---

## 5. Security edge cases

- **Credentials file perms — handled well:** `utils/credentials.py:144,150` writes
  secrets with `os.open(..., 0o600)` and re-`chmod`s to 600 (defence-in-depth, fixes
  pre-existing loose perms on write). Fallback storage is Fernet (`:125`) with a key
  derived via scrypt KDF from a per-install random secret (`:176`). ✅ (Minor: no
  warning is emitted if an existing creds file is found world-readable *before* the
  next write.)
- **Keyring unavailable — handled:** graceful fallback to encrypted file storage with
  logged warnings (`credentials.py:36,107-120`). ✅
- **Encryption key corrupted:** decrypt failures are caught and treated as
  missing-credential (sessions/credentials `except Exception` paths) — fails safe,
  though silently; a corrupt fallback key makes all credentials silently disappear
  (comment acknowledges this at `credentials.py:141`).
- **M1 — CRITICAL (MCP):** the server is **fail-open** — `_guardrail_block`
  (`mcp/server.py:604-641`) only blocks mutating tools if `XPST_MCP_READONLY` /
  `XPST_MCP_REQUIRE_CONFIRM` are explicitly set; with neither set (the default) every
  mutating tool (`xpst_post`, `xpst_delete`, `kb_add`, …) runs with no confirmation
  and no auth over stdio.
- **M2 — CRITICAL (MCP path traversal):** `workspace` flows unvalidated into
  `Workspace.resolve` (`knowledge/workspace.py:20-27`) — `"../../../tmp/evil"` escapes
  the knowledge dir and is created/written. Same for `video_id` in `xpst_transcript`
  (`server.py:1133-1158` → `workspace.py:69-81`) — `"../../../../etc/passwd"`-style
  values read arbitrary `.json` files and return their contents.
- **M6 — HIGH (SSRF, MCP):** `kb_add` `source` is fed straight to
  `yt_dlp.extract_info(url, download=True)` (`knowledge/ingest/resolve.py:48`) — an
  agent can make the server fetch internal/metadata URLs
  (`http://169.254.169.254/...`) or ingest arbitrary local files.
- **P8 — HIGH (SSRF, platforms):** Instagram Graph (`instagram.py:219-223,293-324`)
  and Threads (`threads.py:174-200`) pass an untrusted `video_url` straight through
  with `http://` accepted and no host allow-list. (Fetch happens server-side at
  Meta, but cleartext + no validation is still wrong.)
- **M7 — HIGH (MCP):** audit logging is best-effort fail-open
  (`utils/audit_logger.py:77-82`) — a full disk/unwritable dir only emits a
  `warning`; destructive tool actions proceed unrecorded, and the audit call runs
  *after* the mutation (`server.py:714`) so it can never gate.
- **M8 — MEDIUM (MCP):** `xpst_security_audit` (`server.py:1076-1102`) reports several
  checks (`mcp_readonly`, `dashboard_localhost`, `encrypted_storage`) as hardcoded
  `passed:True` — it tells the operator readonly is on even when it is off, masking
  M1.

---

## 6. Performance

- **U8 — HIGH (analytics N+1):** Instagram (`analytics.py:275-318`, 2 calls/post),
  X (`:342-357`, 1/post), TikTok/Threads (`:381-484`, a full
  `yt_dlp.extract_info` per post) all loop one network call per post. Only YouTube
  batches (50 ids/request, `:220-226`). 1,000 posts → 1,000+ sequential round-trips.
- **U1 — HIGH (sync I/O on async path):** the 300s ffmpeg encode
  (`upload_service.py:259→651→video.py:273`) and the 30s ffprobe calls
  (`upload_service.py:240,282,634`) run synchronously inside `async def` with no
  `to_thread`/`run_in_executor` — they freeze the whole event loop. The analytics
  collectors (`analytics.py:216-484`) are likewise blocking inside `asyncio.gather`,
  so the docstring's "parallel fetching" (`analytics.py:67`) is false: platforms run
  serially.
- **U9 — MEDIUM (10k+ videos):** `analytics._discover_post_ids`
  (`analytics.py:498-525`) `json.load`s the entire `state.json` and nested-loops
  every video × platform on every `collect_all`. State is a single growing JSON blob
  (`state_store.py`), so memory and scan time grow unbounded. `source_service.filter_new`
  (`source_service.py:155-164`) is O(videos×platforms) but bounded by `max_count`.
- **U10 — LOW:** `analytics_store.latest()/totals_before()` (`:124-178`) self-join +
  `GROUP BY MAX(captured_at)` with no `(platform, post_id, captured_at)` composite
  index degrades at hundreds of thousands of snapshots.
- **No unbounded recursion** found. **M1 (YouTube) — MEDIUM:** `_execute_upload`
  (`youtube.py:267-275`) `while response is None: request.next_chunk()` has no
  max-iteration / elapsed-time bound — a server that never advances loops forever.

---

## 7. Platform-specific edge cases

| Platform | Question | Verdict |
|----------|----------|---------|
| YouTube | video > 15 min | **Not validated** (`youtube.py:12,68`) — uploaded as a normal video; also forces `privacyStatus:"public"` (`:201-204`) ignoring config |
| Instagram | wrong dimensions | **Not validated** (`instagram.py:10-14`) — no aspect/duration/size check; relies on API error |
| TikTok | token expires mid-upload | **No recovery** — refresh method exists but is never called (P2); also reports success while still `PROCESSING` (`tiktok.py:251-266`) |
| X | cookies expire mid-session | **Partial thread left published**, reported as total failure (P4, `x.py:449-461`) |
| Threads | post too long | Caption truncated (✅) but duration/size limits are dead constants (P6) |

Additional: **P9 — MEDIUM:** `tiktok._is_sandbox()` returns `True` when the platform
is merely *disabled* (`tiktok.py:308-312`), yet `upload()` still performs the real
live post — the "sandbox" flag is cosmetic and does not gate the network call.

---

## 8. Desktop app

- **Q1 — CRITICAL:** `ContentPage.qml:185` — `captionForPost(...)` opens a body and
  immediately declares `function postSelected()` with no statements and no closing
  brace. JS/QML syntax error; the Library page never loads (falls to the
  "Page load error" branch in `main.qml`). *(Confirmed by direct read.)*
- **Q2 — CRITICAL:** `DetailPanel.qml:279,417,533` — `if (cond) { Rectangle {…} }`
  placed directly in a `RowLayout`/object body. QML cannot gate child *object*
  instantiation with a JS `if` statement; this is a parse error and the detail panel
  fails to load. Use `visible:` / `Loader` / `Repeater`. *(Confirmed by direct read.)*
- **Q3 — CRITICAL:** `main.qml:592` calls `controller.getFileInfo(filePath)` — no
  such slot exists on the backend; the drag-drop file-size label is permanently
  broken (caught, but logs on every drop).
- **Q4 — HIGH:** `AnalyticsPage.qml:599-625` — brace imbalance in the Canvas
  `onPaint` JS body (the `if (lastWeekVal > 0)` block at `:605` is never closed);
  malformed function, throws on repaint / Compare mode.
- **H1 — HIGH:** `SettingsPage.qml:748` calls non-existent
  `controller.generateEncodingSample` but the UI shows a "Generating sample…" toast
  regardless — lies to the user.
- **H3 — HIGH:** `notifModel` is never registered in `main.py` though
  `NotificationListModel` exists and `controller.notification` is emitted — the
  entire notifications subsystem is dead (always "0").
- **H4 — HIGH:** `SchedulePage.qml:404` uses `controller.browseForFolder()` (a
  *directory* picker, `backend.py:1693`) and stores the result as the video path →
  schedules a directory, which fails at execution.
- **R3 / H2 / M3** — covered in §1 and §3.
- **Binding loops:** none found. The one self-binding risk (`xpstNoSplash`) was
  deliberately avoided via a differently-named context property (`main.qml:27`).
- **Slot crash-safety:** most `@Slot(result=str)` methods wrap their body in
  try/except and return a JSON error payload, so a raising slot generally does **not**
  crash the app. Exceptions: `getLocalVideos` has an unguarded `f.stat()` loop
  (`backend.py:1676,1686`) that raises if a file vanishes mid-scan (L3, LOW).

---

## 9. MCP server

Covered in §5 (M1, M2, M6, M7, M8). Additional:

- **M9 — HIGH:** No runtime argument validation — the low-level `call_tool` decorator
  does not enforce `inputSchema`, so `_handle_post` (`server.py:802-823`) does
  `args["video_path"]` / `args["caption"][:100]` directly → `KeyError`/`TypeError`
  on missing or wrong-typed args; `xpst_search` `limit` (`server.py:1164`) is
  unbounded.
- **M10 — HIGH:** `xpst_delete` (`server.py:1267-1298`) uses `args["video_id"]` with
  no existence check and returns `success:True, removed:[]` for a nonexistent id —
  misleading and, combined with M1, lets any agent iterate to wipe state.
- **M11 — MEDIUM:** `kb_add` leaks temp dirs — `tempfile.mkdtemp` per call
  (`resolve.py:45`); failed downloads clean up but **successful** ones never do.
- **M12 — LOW:** the catch-all returns `f"Error: {str(e)}"` (`server.py:717-724`),
  which can leak absolute `~/.xpst/...` paths and internal structure to the agent.

---

## 10. Dependency risks

**`pyproject.toml`** — dependencies are generally well-pinned with upper bounds
(`<` caps on click, pyyaml, fastapi, twikit, instagrapi, etc.), with explicit CVE
notes (`pydantic-settings>=2.14.2`, `msgpack>=1.2.1`). Findings:

- **D1 — MEDIUM:** `yt-dlp>=2025.1.1` is intentionally **unpinned** (no upper bound),
  with a comment explaining it must track platform changes. This is a defensible
  trade-off (a stale yt-dlp breaks downloads), but it means an arbitrary future
  release — including a breaking API change or a regression — is auto-pulled. yt-dlp
  is also the engine behind the MCP SSRF/arbitrary-download surface (M6) and the
  per-post analytics N+1 (U8). Mitigation: pin to a tested minimum *range* with a
  loose upper cap and update deliberately, or run a smoke test on upgrade.
- **D2 — LOW:** `instagrapi` (unofficial Instagram client) and `twikit` (unofficial X
  client) are reverse-engineered libraries; both are inherently fragile to platform
  changes and are the basis for the "cookies/session expire mid-upload" failure
  modes (P4). Not a packaging defect, but a standing operational risk worth
  documenting for users.
- No other dependency carries a *known* unaddressed advisory in the declared ranges.

---

## Appendix — verified non-issues

- **httpx.AsyncClient leaks:** none — all uses are `async with`.
- **Bare `open()` without context manager:** none in `src/xpst/` — grep hits were
  method names / function params.
- **QML binding loops:** none found.
- **State write atomicity against process kill:** `os.replace` is atomic; a SIGKILL
  cannot corrupt `state.json` (only power loss can — R6).
- **Caption oversize truncation:** consistently handled on all five platforms.
- **`yaml.safe_load`** is used (not `yaml.load`) — no code-execution risk from config.

---

*All findings are read-only observations. No source files were modified.*
