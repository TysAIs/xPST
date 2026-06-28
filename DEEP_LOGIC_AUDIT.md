# xPST Deep Logic Audit

**Date:** 2026-06-28  
**Scope:** Full codebase audit — state management, analytics, architecture, engine, desktop app, enterprise readiness  
**Files examined:** state.py, state_manager.py, state_store.py, engine.py, analytics.py, analytics_store.py, dashboard/analytics.py, services/upload_service.py, connect.py, desktop_app/backend.py, desktop_app/models.py, desktop_app/qml/main.qml, desktop_app/qml/pages/ContentPage.qml, desktop_app/qml/pages/DetailPanel.qml, desktop_app/qml/pages/AnalyticsPage.qml, desktop_app/qml/pages/DashboardPage.qml

---

## Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 4 |
| HIGH | 11 |
| MEDIUM | 14 |
| LOW | 6 |

The most impactful cluster is the **analytics data pipeline**, which is broken end-to-end: post IDs are stored under the wrong key name → auto-discovery silently returns empty → DetailPanel uses wrong field names to find posts → all per-video metrics display 0. Platform coverage gaps compound this: engine supports 6 platforms but state schema, analytics, health dashboard, connect wizard, and all QML UI components only cover 4.

---

## Area 1: Content Database / Video Library

### CRITICAL-1 — Dual StateStore instances on same file path
**File:** `src/xpst/state.py:32-33`

```python
self._new_manager = NewStateManager(config_dir)  # creates StateStore internally
self._store = StateStore(config_dir)              # SECOND StateStore on same path
```

`NewStateManager.__init__` creates its own `StateStore(config_dir)` internally. `state.py` then also creates `self._store = StateStore(config_dir)`, opening a second independent StateStore on the same `state.json` file. Each store holds its own `_thread_lock` (a `threading.RLock`) and its own in-memory `_state` dict. A write via `self._new_manager._store` is invisible to `self._store` until both re-read from disk. If any code path writes through `self._store` (e.g., `_acquire_file_lock` / `_release_file_lock` path) concurrently with a write through `self._new_manager`, the two thread locks do not serialize against each other, creating a race condition that produces lost writes.

The `self._store` exists solely to provide `_lock_fd`, `_acquire_file_lock()`, and `_release_file_lock()` — all of which only the test suite uses. In production it is an orphaned instance that adds no value and introduces the dual-lock hazard.

**Fix:** Remove `self._store = StateStore(config_dir)` from `state.py`. Delegate `_acquire_file_lock` / `_release_file_lock` to `self._new_manager._store`.

---

### CRITICAL-2 — analytics.py auto-discovery always returns empty (wrong key name)
**File:** `src/xpst/analytics.py:430`

```python
if info.get("post_id"):   # BUG: state.json stores "id", not "post_id"
    post_ids[platform].append(info["post_id"])
```

State JSON stores platform entries as:
```json
"posted_to": {"youtube": {"id": "abc123", "url": "...", "timestamp": "..."}}
```

But `_discover_post_ids()` looks for `info.get("post_id")`. The key is `"id"`, not `"post_id"`. The result is that `post_ids` is always `{"youtube": [], "instagram": [], ...}` regardless of how many videos have been posted. `collect_all()` receives empty ID lists and collects no metrics. No error is logged — the method silently succeeds and returns empty data.

**Fix:** Change to `info.get("id")` at line 430.

---

### CRITICAL-3 — DetailPanel.qml loadPostData() never populates postData (wrong field names)
**File:** `src/xpst/desktop_app/qml/pages/DetailPanel.qml:33-39`

```javascript
if (posts[i].video_id === postId || posts[i].id === postId) {
```

`controller.recentPosts` JSON (from `backend.py._refresh_recent_posts()`) uses:
- `"title"` field for video_id  
- `"postId"` field for the platform-specific post ID  

Neither `video_id` nor `id` exist as keys in the recentPosts records. The loop finds no match for any `postId`. `postData` stays `{}`. Every downstream read of `postData.platforms`, `postData.views`, etc. returns undefined or 0. All per-video metrics in DetailPanel permanently display 0.

**Fix:** Change the comparison to `posts[i].title === postId || posts[i].postId === postId`, OR change `backend.py` to emit `video_id` as a field name instead of `title`.

---

### CRITICAL-4 — engine.py backfill() stale state reference
**File:** `src/xpst/engine.py:752`

```python
for video_id, video_data in self.state.state["posted_videos"].items()
```

`self.state` is the `state.py` StateManager wrapper. `state.py:38` sets `self.state.state = self._new_manager._state` at **init time** — this captures the dict reference returned by the `_state` property. If any state updater replaces the dict object (returns a new dict from the lambda), the reference stored at `state.py:38` becomes stale, and `backfill()` would iterate over an old snapshot. While current updaters mutate in place (so this works today), it is one refactor away from silently iterating stale data.

Additionally, accessing `.state` on the StateManager wrapper is accessing a private implementation detail exposed only for test compatibility. Any rename of this attribute would break backfill silently.

**Fix:** Replace `self.state.state["posted_videos"]` with `self.state.get_video(video_id)` in a loop over `self.state.list_video_ids()`, or access via the public API.

---

### HIGH-1 — Thumbnails always empty in Content Library
**File:** `src/xpst/desktop_app/backend.py:499` and `src/xpst/desktop_app/models.py:134`

`backend.py:499`:
```python
"thumbnail": vdata.get("thumbnail") or ""  # state.json never stores "thumbnail"
```

`models.py:134`:
```python
"thumbnail": url  # uses the post URL (e.g. https://youtube.com/watch?v=...) as thumbnail
```

`getThumbnail(url)` calls ffmpeg to extract a frame at 1 second from the file. ffmpeg cannot extract a frame from a remote URL like a YouTube video page URL. It returns `""`. ContentPage.qml:1870 calls `controller.getThumbnail(modelData.thumbnail)` and receives an empty string for every posted video. The content library shows no thumbnails.

**Fix:** Store thumbnail path in state.json when a video is downloaded (currently tracked only in download temp vars, not persisted). Or implement a proper thumbnail cache keyed on video_id.

---

### MEDIUM-1 — PostListModel creates one row per platform per video
**File:** `src/xpst/desktop_app/models.py` (inferred from summary)

A video cross-posted to 3 platforms produces 3 rows in the content library with identical titles. ContentPage.qml's dedup detection (`duplicateTitles`) flags these as duplicates and shows a warning UI. This is technically intentional (one row per platform post) but confuses users and triggers false duplicate warnings.

---

## Area 2: Per-Video Analytics in UI

### HIGH-2 — DetailPanel.qml onDeleteComplete never fires
**File:** `src/xpst/desktop_app/qml/pages/DetailPanel.qml:96-110`

```javascript
Connections {
    function onDeleteComplete(resultJson) {  // listens for "deleteComplete" signal
```

`AppController` in `backend.py` emits `postComplete`, not `deleteComplete`. The `Connections` block listens for a signal that never fires. After deleting a post, DetailPanel never refreshes — the deleted post data stays on screen until the user navigates away.

**Fix:** Change `onDeleteComplete` to `onPostComplete` and verify the emitted signal name in backend.py.

---

### HIGH-3 — DetailPanel.qml platformList() falls back to hardcoded 4 platforms
**File:** `src/xpst/desktop_app/qml/pages/DetailPanel.qml:50-61`

Because `postData` is always `{}` (see CRITICAL-3), `postData.platforms` is undefined. `platformList()` falls back to `["youtube", "instagram", "x", "tiktok"]` for every post. A video posted only to YouTube and LinkedIn would show 4 tabs including two with no data, and omit LinkedIn entirely.

---

### HIGH-4 — AnalyticsPage.qml and DashboardPage.qml missing Threads/LinkedIn
**Files:** `src/xpst/desktop_app/qml/pages/AnalyticsPage.qml:199-204`, `src/xpst/desktop_app/qml/pages/DashboardPage.qml:354`

AnalyticsPage platform tabs:
```javascript
{ name: "All" }, { name: "YouTube" }, { name: "Instagram" }, { name: "X" }, { name: "TikTok" }
// No "Threads", no "LinkedIn"
```

DashboardPage health grid:
```javascript
var keys = ["youtube", "instagram", "x", "tiktok"]
```

If the engine successfully posts to Threads or LinkedIn, neither page will show their data. Posts to those platforms are silently excluded from all aggregate metrics and health status.

---

### MEDIUM-2 — analytics.py (top-level) _collect_platform() missing Threads and LinkedIn
**File:** `src/xpst/analytics.py`

`_collect_platform()` only handles: `youtube`, `instagram`, `x`, `tiktok`. No collection for `threads` or `linkedin`. If Threads/LinkedIn posts exist in state, their `post_id` values are never looked up for metrics.

---

### MEDIUM-3 — dashboard/analytics.py only initializes 4 platforms throughout
**File:** `src/xpst/dashboard/analytics.py:36-57, 701, 832, 786`

`PLATFORM_COLORS`, `PLATFORM_ICONS`, `PLATFORM_LABELS`, `get_summary_stats()` `platform_counts`, `get_engagement_data()` `engagement` dict, and `get_platform_health_all()` all hardcode the same 4-platform list. Threads and LinkedIn data is silently dropped at every aggregation step.

---

## Area 3: Architecture Quality

### HIGH-5 — Platform coverage inconsistency across all layers (4 vs 6 platforms)
**Files:** Multiple

Engine (`engine.py`) initializes and supports 6 platforms. Every other layer covers only 4:

| Layer | Platforms covered | Missing |
|-------|-------------------|---------|
| `state_store.py:247-255` `_ensure_state_keys()` | youtube, x, instagram | tiktok, threads, linkedin |
| `state_store.py:264-274` `_empty_state()` | youtube, x, instagram | tiktok, threads, linkedin |
| `state_manager.py:388` `get_statistics()` `by_platform` | youtube, x, instagram, tiktok | threads, linkedin |
| `backend.py:168` `PLATFORMS` | youtube, instagram, x, tiktok | threads, linkedin |
| `dashboard/analytics.py` (all methods) | youtube, instagram, x, tiktok | threads, linkedin |
| `analytics.py` `_collect_platform()` | youtube, instagram, x, tiktok | threads, linkedin |
| `connect.py:752` `all_platforms` | tiktok, youtube, instagram, x | threads, linkedin |
| `ContentPage.qml` filter pills | All, YouTube, Instagram, X, TikTok | Threads, LinkedIn |
| `ContentPage.qml` batch caption | YouTube, Instagram, X, TikTok | Threads, LinkedIn |
| `AnalyticsPage.qml` tabs | All, YouTube, Instagram, X, TikTok | Threads, LinkedIn |
| `DashboardPage.qml` health grid | youtube, instagram, x, tiktok | threads, linkedin |
| `DetailPanel.qml` platformDisplayName() | youtube, instagram, x, tiktok | threads, linkedin |

A video posted to Threads or LinkedIn via the engine will: have no health entry initialized, not appear in any analytics aggregate, not appear in the health dashboard, have no filter pill in the content library, not be connectable via the connect wizard, and not be shown in DetailPanel tabs.

---

### MEDIUM-4 — state.py mark_video_posted() calls private method directly
**File:** `src/xpst/state.py:170`

```python
self._new_manager._add_posted_video_inner(
    self._state,
    ...
)
```

`_add_posted_video_inner` is a private method (underscore-prefixed). This call bypasses the `StateStore.update()` locking mechanism — the state dict is mutated directly without holding `_file_lock()`. Concurrent writes to the same video record from two threads (e.g., engine uploading to two platforms simultaneously) could produce a partial write that is then atomically persisted as corrupted data.

---

### MEDIUM-5 — engine.py _cleanup_encoded_files() skips TikTok/Threads/LinkedIn temp files
**File:** `src/xpst/engine.py:53`

```python
_ENCODED_SUFFIXES = ("_youtube", "_instagram", "_x")
```

`_cleanup_encoded_files()` iterates this tuple. Encoded files named `video_tiktok.mp4`, `video_threads.mp4`, `video_linkedin.mp4` are never matched and never cleaned up. They accumulate on disk indefinitely.

**Fix:** Add `"_tiktok"`, `"_threads"`, `"_linkedin"` to `_ENCODED_SUFFIXES`.

---

### LOW-1 — knowledge module integration is aspirational only
**Files:** `src/xpst/analytics_store.py:6-12`, `src/xpst/knowledge/`

`analytics_store.py` documents a "JOIN CONTRACT" with the knowledge module ("a knowledge nugget resolves to its performance history through (source_platform, source_post_id)"). The actual join logic does not exist in the codebase. The knowledge module (LanceDB vector store, course assembly) is a fully separate subsystem with no live calls from the analytics pipeline or desktop app.

---

## Area 4: Logic Bugs

### HIGH-6 — connect.py has no connect flows for Threads or LinkedIn
**File:** `src/xpst/connect.py:752, 761-765`

```python
all_platforms = ["tiktok", "youtube", "instagram", "x"]
platform_connectors = {
    "tiktok": connect_tiktok,
    "youtube": connect_youtube,
    "instagram": connect_instagram,
    "x": connect_x,
}
```

`xpst connect threads` or `xpst connect linkedin` prints "Unknown platform" and returns False. There is no OAuth or credential setup flow for either platform despite the engine being able to post to both. Users have no supported path to authenticate these platforms.

---

### HIGH-7 — backend.py getFileInfo() slot missing
**File:** `src/xpst/desktop_app/backend.py` (absent), `src/xpst/desktop_app/qml/main.qml:578`

`main.qml:578`:
```javascript
try {
    var info = controller.getFileInfo(filePath)
} catch(e) { /* swallowed */ }
```

`getFileInfo()` is not defined anywhere in `AppController` (2215-line `backend.py`). The try/catch silently swallows the error. The file info section in the drag-and-drop UI (file size, duration) never populates.

**Fix:** Implement `getFileInfo()` as a `@Slot` in `AppController`, or remove the call and the UI that depends on it.

---

### HIGH-8 — backend.py PLATFORMS tuple drives health updates but omits Threads/LinkedIn
**File:** `src/xpst/desktop_app/backend.py:168`

```python
PLATFORMS = ("youtube", "instagram", "x", "tiktok")
```

This constant gates `_refresh_platform_health()`, the health status properties exposed to QML, and likely the `connectPlatform()` flow. Engine posts to Threads/LinkedIn never register a health state change in the desktop UI.

---

### MEDIUM-6 — upload_service.py carousel pipeline missing anti-bot checks
**File:** `src/xpst/services/upload_service.py:487-605`

`upload_carousel_to_platform()` has no anti-bot time-of-day check, no daily limit check, no between-platform wait, and no caption variation — all of which `upload_to_platform()` has at lines 112-166. Carousel uploads bypass the human-behavior simulation entirely.

---

### MEDIUM-7 — upload_service.py TikTok/Threads/LinkedIn use Instagram encoding config
**File:** `src/xpst/services/upload_service.py:622-624`

```python
elif platform in ("tiktok", "threads", "linkedin"):
    config = self.config.video.encoding_instagram
```

All three platforms share Instagram's encoding profile. While compatible, this prevents per-platform tuning (e.g., LinkedIn prefers wider aspect ratios, TikTok prefers portrait). No platform-specific codec or bitrate targets are applied.

---

### MEDIUM-8 — connect.py Instagram challenge handler uses non-existent method
**File:** `src/xpst/connect.py:362`

```python
client.challenge_code_handler(username, code)
```

`instagrapi.Client` v2 does not have a `challenge_code_handler()` method. This is an instagrapi v1 API call. On v2, calling this will raise `AttributeError`. Instagram challenge verification (SMS/email code) silently fails for any user who hits a login challenge.

---

### MEDIUM-9 — state_manager.py is_circuit_breaker_open() reads state without lock
**File:** `src/xpst/state_manager.py:374-379`

```python
def is_circuit_breaker_open(self, platform: str) -> bool:
    threshold = 5  # Could be configurable
    state_obj = self._state  # no lock
    platform_state = state_obj["health"]["platforms"].get(platform, {})
    return platform_state.get("failures", 0) >= threshold
```

`self._state` calls `self._store.get_raw()` which returns the raw dict without acquiring `_thread_lock`. A concurrent write in another thread could produce a torn read. In the upload service, `is_circuit_breaker_open` is called before each upload — a torn read could allow an upload to proceed when the circuit breaker should block it.

---

### LOW-2 — state_manager.py circuit breaker threshold hardcoded
**File:** `src/xpst/state_manager.py:376`

```python
threshold = 5  # Could be configurable
```

The comment acknowledges this should be configurable but it isn't. The threshold cannot be changed without modifying source code. It also resets via `update_platform_health(platform, True)` after any single success (line 319), which is aggressive — one successful upload re-enables the circuit breaker even after 4 failures.

---

### LOW-3 — engine.py _ENCODED_SUFFIXES cleanup gap (also noted in MEDIUM-5)
Already captured as MEDIUM-5.

---

## Area 5: Desktop App Completeness

### HIGH-9 — DetailPanel.qml has duplicate import and pinned QtMultimedia version
**File:** `src/xpst/desktop_app/qml/pages/DetailPanel.qml:1-6`

```qml
import xpst.desktop_app.qml 1.0    // line 1
import QtQuick 2.15
import xpst.desktop_app.qml 1.0    // line 3 — DUPLICATE
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import QtMultimedia 5.15            // version pinned
```

The duplicate import is redundant (QML ignores duplicates silently). The `QtMultimedia 5.15` pin is inconsistent — ContentPage.qml uses `import QtMultimedia` with no version. If the Qt installation only has QtMultimedia 6.x, this import may fail at runtime or load the wrong API version.

---

### HIGH-10 — AnalyticsPage.qml has duplicate import
**File:** `src/xpst/desktop_app/qml/pages/AnalyticsPage.qml:1-3`

```qml
import xpst.desktop_app.qml 1.0   // line 1
import QtQuick 2.15
import xpst.desktop_app.qml 1.0   // line 3 — DUPLICATE
```

Same pattern as DetailPanel.qml.

---

### MEDIUM-10 — ContentPage.qml filter pills and batch caption missing Threads/LinkedIn
**Files:** `src/xpst/desktop_app/qml/pages/ContentPage.qml:1630`, ContentPage.qml batch caption dialog

Filter pills: `["All", "YouTube", "Instagram", "X", "TikTok"]`  
Batch caption platforms: `[YouTube, Instagram, X, TikTok]`

Posts to Threads or LinkedIn cannot be filtered or batch-captioned in the UI.

---

### MEDIUM-11 — ContentPage.qml O(n²) similarity score on every render
**File:** `src/xpst/desktop_app/qml/pages/ContentPage.qml` (dedup/similarity logic)

Similarity scores are computed on every card for every render pass. With a large content library, this creates an O(n²) computation in the QML engine's JavaScript thread, causing visible UI lag when scrolling.

---

### MEDIUM-12 — main.qml navigateTo("detail") passes no postId to DetailPanel
**File:** `src/xpst/desktop_app/qml/main.qml`

When navigating to the detail page, `navigateTo("detail")` is called without setting `detailPage.postId`. DetailPanel shows the empty state ("No post selected") until `postId` is set separately. If the navigation mechanism doesn't set `postId` before rendering, the detail page is always empty.

---

### MEDIUM-13 — backend.py _captionUpdateReady signal defined after method that emits it
**File:** `src/xpst/desktop_app/backend.py:1746, 1759`

`updateCaption()` method at line 1746 emits `self._captionUpdateReady`. The signal `_captionUpdateReady = Signal(str, str, str)` is defined at line 1759. In Python, class body is executed top-to-bottom. If `updateCaption()` were ever called during `__init__` (currently it's not), it would fail with `AttributeError` because the signal object doesn't exist yet at method definition time. The ordering is fragile.

---

### LOW-4 — connect.py TikTok only verifies yt-dlp is installed, not actual upload capability
**File:** `src/xpst/connect.py:527-575`

The TikTok "connect" flow configures username and browser cookies but only checks `shutil.which("yt-dlp")`. It never tests that the configured username's videos are actually accessible or that cookie extraction works. A user can complete "connect tiktok" successfully even if cookies are expired or the username is wrong.

---

### LOW-5 — Onboarding / Connect / Settings / Schedule / Compose / About pages not audited
The following QML pages were not fully read during this audit:
- `ConnectPage.qml`
- `ComposePage.qml`
- `SchedulePage.qml`
- `SettingsPage.qml`
- `OnboardingPage.qml`
- `AboutPage.qml`

These should be audited as a follow-up, particularly ConnectPage.qml (does it expose Threads/LinkedIn?) and SchedulePage.qml (does it handle timezone-aware scheduling correctly?).

---

## Area 6: Enterprise Readiness

### HIGH-11 — state_store.py _ensure_state_keys() and _empty_state() only initialize 3 platforms
**File:** `src/xpst/state_store.py:247-255, 264-274`

`_ensure_state_keys()`:
```python
state.setdefault("health", {
    "platforms": {
        "youtube": {...},
        "x": {...},
        "instagram": {...},
        # tiktok, threads, linkedin missing
    },
    ...
})
```

`_empty_state()` has the same gap. A fresh state.json, or one recovered from a backup, will have no health entries for tiktok, threads, or linkedin. Calls to `state["health"]["platforms"][platform]` from `_record_failure_inner()` (state_manager.py:198) check `if platform in state["health"]["platforms"]` before updating — so tiktok/threads/linkedin failures are silently not recorded in health. Platform health tracking is broken for 3 of the 6 platforms from the first run.

**Fix:** Add tiktok, threads, linkedin to both `_ensure_state_keys()` and `_empty_state()`.

---

### MEDIUM-14 — dashboard/analytics.py uses hardcoded default path "~/.xpst"
**File:** `src/xpst/dashboard/analytics.py:149`, `src/xpst/dashboard/analytics.py:60`

```python
def __init__(self, config_dir: str = "~/.xpst") -> None:
def load_state(config_dir: str = "~/.xpst") -> dict[str, Any]:
```

If `config_dir` is overridden (e.g., custom install path, test environment), callers must pass it explicitly. The backend.py creates `AnalyticsCollector(self.config.config_dir)` — correct. But `load_state()` called without arguments in some internal paths would fall back to `~/.xpst` regardless of the actual config.

---

### LOW-6 — state.py mark_video_posted() does not persist via StateStore.update()
**File:** `src/xpst/state.py:169-178`

```python
with self._new_manager._save_lock:
    self._new_manager._add_posted_video_inner(
        self._state,  # mutates dict directly
        ...
    )
```

`_add_posted_video_inner()` mutates `self._state` (the raw dict) but does not call `self._new_manager._store.save()` or `_atomic_write()` afterward. If the process crashes before the next save cycle, the marked-as-posted record is lost. The next run would re-post the video. This path is used by `upload_service.py:330` via `self.state.mark_video_posted()`.

**Fix:** After the mutation, call `self._new_manager.save()` to atomically persist the change.

---

## Cross-Cutting Issue: Analytics End-to-End Pipeline is Broken

These findings compose into a single broken pipeline:

```
1. state.json stores posted_to[platform]["id"]
                                         ↑
2. analytics.py _discover_post_ids() reads info.get("post_id")  ← WRONG KEY (CRITICAL-2)
   → always returns empty lists
   → collect_all() is never called with any IDs
   → AnalyticsStore records nothing

3. backend.py _refresh_recent_posts() produces recentPosts JSON
   with "title" as video_id field

4. DetailPanel.qml loadPostData() looks for posts[i].video_id  ← WRONG FIELD (CRITICAL-3)
   → postData stays {}
   → platformList() falls back to hardcoded 4 platforms
   → all metrics display 0
```

Even if CRITICAL-2 and CRITICAL-3 are fixed individually, AnalyticsPage will still show only 4 platforms (HIGH-4), Threads/LinkedIn health will be absent from DashboardPage (HIGH-4), and connect.py won't be able to connect Threads/LinkedIn (HIGH-6).

---

## Fix Priority Order

1. **CRITICAL-2** (`analytics.py:430`): `info.get("post_id")` → `info.get("id")` — one-line fix, unblocks all analytics collection
2. **CRITICAL-3** (`DetailPanel.qml:33`): `posts[i].video_id` → `posts[i].title` — one-line fix, unblocks per-video detail view
3. **HIGH-11** (`state_store.py:247-274`): add tiktok/threads/linkedin to platform health schema — 6-line fix, prevents silent health-tracking failures
4. **MEDIUM-5** (`engine.py:53`): add `_tiktok`, `_threads`, `_linkedin` to `_ENCODED_SUFFIXES` — prevents temp file accumulation
5. **HIGH-5** (platform coverage): systematic pass to add threads/linkedin to all 4-platform enumerations — required before Threads/LinkedIn is usable end-to-end
6. **CRITICAL-1** (`state.py:33`): remove orphan `self._store = StateStore(config_dir)` — eliminates dual-lock race
7. **HIGH-7** (`backend.py`): implement `getFileInfo()` `@Slot` — fixes drag-and-drop file info display
8. **HIGH-2** (`DetailPanel.qml:98`): `onDeleteComplete` → `onPostComplete` — fixes delete refresh
9. **LOW-6** (`state.py:178`): add `self._new_manager.save()` after `_add_posted_video_inner()` — prevents post-loss on crash
10. **HIGH-1** (thumbnails): persist thumbnail path in state.json at download time — enables content library thumbnails
