# xPST Audit Findings
**Date:** 2026-06-28  
**Auditor:** Claude (automated multi-agent audit)  
**Scope:** Password prompts, video quality, TikTok posting, UI animations, KB, scheduling, analytics, OSS attribution

---

## Severity Legend
- **CRITICAL** — Data loss, security vulnerability, or feature completely broken
- **HIGH** — Feature significantly degraded or user-facing bug affecting core workflow
- **MEDIUM** — Feature works but with gaps, missing polish, or incorrect behavior in edge cases
- **LOW** — Minor inconsistency, documentation gap, or improvement opportunity

---

## 1. PASSWORD PROMPT ISSUE

### Finding 1.1 — `launchctl bootstrap` is correct; prompts likely from macOS Keychain (MEDIUM)

**Files:**
- `src/xpst/cli.py:2559` — `launchctl bootstrap gui/{uid} {plist_path}` (install)
- `src/xpst/cli.py:2565` — `launchctl bootout gui/{uid}/com.xpst.schedule` (uninstall)
- `src/xpst/cli.py:2673` — `launchctl unload {plist_path}` (legacy uninstall path)

**Findings:**  
The `schedule install` command uses `launchctl bootstrap` (modern, unprivileged) rather than the legacy `launchctl load`. `bootstrap` operates on user-space `gui/{uid}` agents and does **not** require elevated privileges. The LaunchAgent plist is written to `~/Library/LaunchAgents/` (user-writable). No `sudo`, `osascript`, `SMJobBless`, `AuthorizationCreate`, or XPC privilege escalation was found anywhere in the codebase.

**Root cause of password prompts (likely):**  
The `keyring` package (`pyproject.toml` dependency) stores platform credentials in macOS Keychain. On **first access** of a new keychain item — or when the system keychain is locked — macOS shows a password dialog asking to "allow xpst to use the keychain." This is a macOS security policy, not a code bug. It appears as a repeated prompt if:
1. The keychain session has expired between invocations
2. Multiple platforms are connected (each credential access may trigger a separate prompt)
3. The user has their keychain set to lock after inactivity

**Fix needed:**  
- `src/xpst/cli.py:2673` — `launchctl unload` is the legacy form and may emit deprecation warnings on macOS 13+; replace with `launchctl bootout gui/{uid}/com.xpst.schedule`
- For keychain prompts: batch keychain reads at startup with a single "allow always" dialog, or document that users should set their keychain to "always allow" for xpst
- Consider adding a `--no-keyring` fallback mode that reads from a plaintext credentials file (with a warning) for environments where keychain prompts are disruptive

**All subprocess calls found — none require privilege:**

| File | Line | Command | Privilege |
|------|------|---------|-----------|
| `cli.py` | 2559 | `launchctl bootstrap gui/{uid}` | None (user-space) |
| `cli.py` | 2565 | `launchctl bootout gui/{uid}/...` | None (user-space) |
| `cli.py` | 2673 | `launchctl unload {plist_path}` | None (deprecated form) |
| `cli.py` | 590, 611, 687, 710, 712 | `crontab -l/-/r` | None (user crontab) |
| `cli.py` | 2632 | `schtasks` | None (user task, Windows only) |
| `desktop.py` | 150–156 | `sips -s format icns` | None |
| `desktop.py` | 218 | `update-desktop-database` | None (Linux only, graceful fail) |
| `utils/video.py` | 148, 178, 268, 481 | `ffmpeg`, `ffprobe` | None |
| `sources/youtube.py`, `x.py` | various | `yt-dlp` | None |
| `updater.py` | 185, 281, 386, 441, 540 | `pip install`, `ffmpeg -version` | None (venv user install) |
| `desktop_app/backend.py` | 1296 | `python -m xpst mcp` | None |

---

## 2. VIDEO QUALITY

### Finding 2.1 — Encoding profiles are correct for all three active platforms (LOW / INFORMATIONAL)

**File:** `src/xpst/utils/video.py`

| Platform | Function | Lines | Resolution | Quality | Max Rate | GOP | FPS | Audio |
|----------|----------|-------|-----------|---------|----------|-----|-----|-------|
| YouTube | `_build_youtube_cmd()` | 399–452 | 1920 long edge (upscale OK) | 8Mbps CBR | 10Mbps | 15 (closed) | cap 60 | AAC 256k/48kHz |
| Instagram | `_build_instagram_cmd()` | 290–342 | 1920 long edge (no upscale) | CRF 20 | 10Mbps | 72 | cap 60 | AAC 256k/44.1kHz |
| X/Twitter | `_build_x_cmd()` | 344–397 | 1920 long edge (no upscale) | 10Mbps CBR | 12Mbps | 90 | cap 60 | AAC 256k/44.1kHz |

All three profiles use `-fpsmax 60` (FPS **cap**, not force), orientation-aware Lanczos scaling, `yuv420p`, `bt.709` colorspace, `-movflags +faststart`, and `-preset slow`. These match platform specifications.

### Finding 2.2 — TikTok raises `ValueError` in `encode_for_platform()` but is correctly handled upstream (LOW)

**File:** `src/xpst/utils/video.py:265`  
```python
raise ValueError(f"Unknown platform: {platform}")
```

**File:** `src/xpst/services/upload_service.py:622–625`  
```python
elif platform in ("tiktok", "threads", "linkedin"):
    # New platforms: passthrough — they accept standard MP4 and
    # perform their own re-encoding server-side.
    return video_path
```

**Assessment:** Not a bug — the passthrough in `upload_service.py` means `encode_for_platform()` is never called for TikTok/Threads/LinkedIn. However, the `ValueError` in `video.py:265` is a footgun: any future code that calls `encode_for_platform("tiktok")` directly will crash with an opaque error rather than a graceful passthrough. The `ValueError` should be replaced with a handled passthrough or the platform list should be kept in sync.

**Fix needed:** In `src/xpst/utils/video.py:265`, add TikTok/Threads/LinkedIn to a passthrough branch rather than raising `ValueError`. Or document the invariant that these platforms must never reach `encode_for_platform()`.

### Finding 2.3 — No double-encoding or quality loss paths detected (INFORMATIONAL)

**File:** `src/xpst/services/upload_service.py:647–649`  
```python
if output_path.exists() and output_path.stat().st_size > 1000:
    return output_path
```

Caching prevents re-encoding of already-encoded outputs. Carousel stitching (`upload_service.py:488–638`) encodes once directly to CRF 20 / 1080×1920 and then follows the normal pipeline — no double encode. Compliance probing (`is_platform_compliant()`, lines 186–231) skips encoding for already-compliant sources. Pipeline is clean.

---

## 3. TIKTOK POSTING

### Finding 3.1 — TikTok upload works for local files via official API (INFORMATIONAL)

**File:** `src/xpst/platforms/tiktok.py:144–291`

TikTok posting uses the **official TikTok Content Posting API v2** (Direct Post endpoint, `https://open.tiktokapis.com`). Upload is a 3-step container model:
1. `POST /v2/post/publish/video/init/` → get `publish_id` + upload URL
2. `PUT` video bytes to upload URL (single chunk)
3. `POST /v2/post/publish/status/fetch/` → poll until `SUCCESS`

Local files are read directly (`open(video_path, "rb")`). No URL-based upload. OAuth 2.0 with automatic token refresh at `tiktok.py:107–142`. Rate limit: 6 req/min enforced server-side. Caption max: 2,200 chars.

### Finding 3.2 — TikTok IS included in cross-post pipeline (INFORMATIONAL)

**File:** `src/xpst/engine.py:320–327`  
```python
if self.config.tiktok.enabled:
    # TikTok included as destination
```

TikTok is a first-class posting destination. Users can post via `xpst post --video file.mp4 --platforms tiktok` or include it in any multi-platform cross-post.

### Finding 3.3 — TikTok has NO dedicated encoding profile; relies on passthrough (MEDIUM)

**File:** `src/xpst/services/upload_service.py:622–625`

TikTok videos are uploaded **without any xPST re-encoding** — the source video (or whatever Instagram profile produced it if coming from the content pipeline) is sent as-is. TikTok re-encodes server-side.

**Risk:** TikTok's preferred spec is vertical 9:16, H.264, up to 1080×1920, ≤287.6MB, ≤10 minutes. Wide/landscape source videos uploaded without encoding will be letterboxed or cropped by TikTok's server — not controlled by the user. There is also no duration pre-flight check specific to TikTok's limits enforced before upload.

**Fix needed:** Add a `TikTokConfig` profile in `src/xpst/config.py` (matching Instagram's but with TikTok's specific limits) and add a `_build_tiktok_cmd()` in `src/xpst/utils/video.py` — or at minimum, enforce aspect ratio and duration validation before upload so failures are caught locally rather than server-side.

### Finding 3.4 — TikTok posts cannot be deleted programmatically (LOW)

**File:** `src/xpst/platforms/tiktok.py:415–428`

The `delete()` method returns `False` — TikTok Content Posting API does not expose a delete endpoint. Users must manually delete posts in the TikTok app. The engine's state tracking still records the post, but retraction is impossible. This should be documented prominently in the UI.

---

## 4. UI ANIMATIONS / HOVER EFFECTS

### Finding 4.1 — Navigation sidebar is fully polished (INFORMATIONAL)

**File:** `src/xpst/desktop_app/qml/Sidebar.qml`

Nav items: `containsMouse` color change + 120ms `ColorAnimation` + `scale: 1.02` on hover / `0.98` on press with `Behavior` (120ms). Spring-animated selection indicator (`SpringAnimation`, spring: 1.5, damping: 0.4). Notification bell and theme toggle also have hover states. **No action needed.**

### Finding 4.2 — Many interactive elements across 6 pages lack hover states (HIGH)

The pattern below is consistent across multiple pages: buttons are `Rectangle + MouseArea` constructs that only track `cursorShape` but do not respond visually (no `ColorAnimation`, no scale) when hovered.

**Affected files and elements:**

| File | Missing Hover On | Line Reference |
|------|-----------------|----------------|
| `pages/DashboardPage.qml` | Platform health cards, recent posts cards | ~290 (metric cards OK; health/recent cards not) |
| `pages/ContentPage.qml` | Cancel buttons, pagination buttons, filter pills, sort dropdown | ~1687 (batch toolbar region) |
| `pages/ComposePage.qml` | Video selection tiles, platform selection toggles, caption editor | ~333 (browse btn OK; tiles/toggles not) |
| `pages/SettingsPage.qml` | Toggle switches, checkbox form controls, most section controls | ~726, ~900 |
| `pages/ConnectPage.qml` | Platform tiles, auth buttons (partial data — file was truncated) | Unknown |
| `main.qml` | Toast notification close, dialog action buttons | Dialog region |

**Specific patterns missing:**
- Dialog action buttons (`main.qml`): Styled as static `Rectangle`, no `ColorAnimation` on hover
- Filter pills (`ContentPage.qml`): `cursorShape` change only, no background color animation
- Platform toggle tiles (`ComposePage.qml`): Selected state exists but no hover-before-select feedback
- Form control toggles (`SettingsPage.qml`): System-style toggles do not use `theme.accentHover`

**Theme system supports this fix:** `theme.accentHover`, `theme.surfaceAlt`, and `theme.accentMuted` are available. The pattern from `Sidebar.qml` (lines 94–102) and `DashboardPage.qml` (lines 290–292) is the right model:
```qml
color: mouse.containsPress ? theme.accentMuted
     : mouse.containsMouse ? theme.accentHover
     : theme.surface
Behavior on color { ColorAnimation { duration: 120 } }
```

### Finding 4.3 — Page-level entrance animations exist; card entrance animations are inconsistent (MEDIUM)

**File:** `src/xpst/desktop_app/qml/pages/ContentPage.qml:1803–1806`

Content cards have staggered entrance (320ms, 70ms stagger) and scale behavior on hover (1.02, 120ms). But `AnalyticsPage.qml` (lines 374–377, 675–678, 795–798) uses `ParallelAnimation` on entrance for each card group. `DashboardPage.qml` uses a simple opacity fade (200ms). The entrance animation approach is inconsistent across pages — three different patterns. Not broken, but jarring as a user navigates between pages.

### Finding 4.4 — `ConnectPage.qml` and `OnboardingPage.qml` were not fully readable (LOW)

Files exceeded the read window during audit. `ConnectPage.qml` may have hover gaps on platform connection tiles. `OnboardingPage.qml` likely has minimal interactivity. Recommend manual review of both files using the Sidebar pattern as a reference.

---

## 5. KNOWLEDGE BASE

### Finding 5.1 — Knowledge Base is fully functional, not deprecated (INFORMATIONAL)

**Module:** `src/xpst/knowledge/`

KB is an optional feature (`xpst[knowledge]` extra) with soft imports. It is **not deprecated**.

**What's implemented:**
- Transcription: `faster-whisper` (configurable model size)
- Embeddings: `fastembed` with multiple model options
- Vector stores: LanceDB (primary), SQLite FTS, JSON fallback
- Knowledge nuggets: `{point, citation, timestamp_start, timestamp_end, embedding}`
- Knowledge areas: Clustering and organization pipeline
- MCP tools: 4 tools for agent queries (`knowledge/mcp/tools.py`)
- CLI: `xpst kb add|query|reembed|migrate-store|organize`
- Tests: 8 test modules (`test_knowledge_cli.py`, `test_knowledge_extract.py`, etc.)

**Missing:** No desktop QML page. KB is CLI/MCP-only. This appears intentional for the current release — not a gap unless a UI was promised.

---

## 6. SCHEDULING

### Finding 6.1 — Scheduling is fully functional end-to-end (INFORMATIONAL)

**Files:** `src/xpst/schedule_manager.py`, `src/xpst/cli.py:2274`, `src/xpst/desktop_app/qml/pages/SchedulePage.qml`

**What works:**
- `xpst schedule add|list|remove|run|install`
- OS-level integration: macOS launchd (`~/Library/LaunchAgents/com.xpst.schedule.plist`), Linux cron, Windows Task Scheduler
- Desktop UI: Calendar with scheduled-day dot indicators, create form with video picker/caption/platforms/datetime/recurrence, click-day to see posts, per-post delete
- Recurrence: daily/weekly/monthly with month-end clamping
- Persistence: Atomic writes to `~/.xpst/schedule.json`

**No critical gaps found.**

### Finding 6.2 — `launchctl unload` in uninstall path is legacy (LOW)

**File:** `src/xpst/cli.py:2673`  
```python
subprocess.run(["launchctl", "unload", str(plist_path)], ...)
```
`launchctl unload` is deprecated on macOS 13+. Should be `launchctl bootout gui/{uid}/com.xpst.schedule` to match the install path. Not a security issue; may log deprecation warnings.

---

## 7. ANALYTICS

### Finding 7.1 — Analytics is fully functional with real data (INFORMATIONAL)

**Files:** `src/xpst/analytics.py`, `src/xpst/analytics_store.py`, `src/xpst/desktop_app/qml/pages/AnalyticsPage.qml`

**What works:**
- Real metrics from all 4 platforms:
  - YouTube: Data API v3 (viewCount, likeCount, commentCount)
  - Instagram: Business Insights API + fallback to public counts
  - X/Twitter: twikit (views, likes, replies, retweets, quotes, bookmarks)
  - TikTok: yt-dlp metadata extraction (views, likes, comments, reposts)
- SQLite persistent store (`metric_snapshots` table, append-only)
- 15-minute cache TTL; forced refresh via `--refresh`
- Honest comparisons: week-over-week shown only if 7+ days of history exist (no fabricated multipliers)
- Desktop UI: Summary stats, per-platform breakdown, date range picker, bar charts, top posts ranked by engagement, live/cached toggle

**Minor limitations (by design, not bugs):**
- TikTok metrics use yt-dlp (no official analytics API) — best-effort
- Instagram requires Business/Creator account for Insights API; graceful fallback to public counts
- `analytics_store.py` KB contract (join by `source_platform, source_post_id` for performance weighting) is defined but not yet integrated into KB retrieval

---

## 8. OPEN SOURCE ATTRIBUTION

### Finding 8.1 — Attribution is comprehensive and correct (INFORMATIONAL)

**Files:** `pyproject.toml`, `NOTICE.md`, `uv.lock`

All four audited packages are properly declared and attributed:

| Package | Declared In | License | NOTICE.md | Usage |
|---------|------------|---------|-----------|-------|
| yt-dlp | `pyproject.toml` (core dep ≥2025.1.1) | Unlicense | Yes | `sources/tiktok.py`, `setup.py`, `connect.py` — external binary via `shutil.which()` |
| instagrapi | `pyproject.toml` (core dep ≥2.0.0) | MIT | Yes | `platforms/instagram.py` — direct import |
| PySide6 | `pyproject.toml` ([pyside6] optional, ≥6.5.0) | LGPL-3.0 | Yes (LGPL compliance explicitly addressed) | `desktop_app/main.py`, `desktop_app/models.py` — lazy-loaded |
| curl_cffi | `pyproject.toml` ([anti-ban] optional, ≥0.7.0) | MIT | Yes | `anti_bot.py`, `sources/tiktok.py` — optional import with graceful fallback |

**Additional notes:**
- `pyinstaller` (GPL-2.0) is listed as build-only, not distributed — correctly handled
- `pydantic-settings ≥2.14.2` and `msgpack ≥1.2.1` have CVE floor constraints enforced in `pyproject.toml`
- `uv.lock` provides full resolution with hashes for all 188 transitive dependencies
- `yt-dlp` intentionally has no upper bound (comment: "must track platform changes")
- No undeclared dependencies found anywhere in `src/xpst/`
- No license conflicts: all runtime deps are MIT/Apache-2.0/BSD-3-Clause; LGPL (PySide6) is optional

**No remediation needed.**

---

## Summary Table

| # | Area | Finding | Severity | File:Line | Fix Needed? |
|---|------|---------|----------|-----------|-------------|
| 1.1 | Password prompts | No subprocess needs privilege; likely macOS Keychain prompting for credentials | MEDIUM | `cli.py:2673` (legacy unload) | Yes — replace `launchctl unload` with `bootout`; batch Keychain reads |
| 2.2 | Video quality | TikTok/Threads/LinkedIn `ValueError` in `encode_for_platform()` is a footgun | LOW | `utils/video.py:265` | Yes — replace `ValueError` with passthrough or sync platform list |
| 2.3 | Video quality | No double-encoding; caching and compliance passthrough work correctly | INFO | `upload_service.py:647` | No |
| 3.3 | TikTok | No dedicated TikTok encoding profile; passthrough risks letterboxing landscape sources | MEDIUM | `upload_service.py:622` | Yes — add TikTok profile or pre-flight aspect ratio check |
| 3.4 | TikTok | Cannot delete TikTok posts (API limitation) | LOW | `platforms/tiktok.py:415` | Document in UI; no code fix possible |
| 4.2 | UI hover | 6 pages have interactive elements with no hover visual feedback | HIGH | Multiple QML files (see §4.2) | Yes — apply `containsMouse + ColorAnimation` pattern |
| 4.3 | UI animations | Page entrance animation patterns inconsistent across 3 styles | MEDIUM | Multiple QML pages | Yes — standardize to one pattern |
| 4.4 | UI hover | `ConnectPage.qml` and `OnboardingPage.qml` not fully audited | LOW | Unknown | Manual review needed |
| 5.1 | Knowledge Base | Fully functional, not deprecated; no desktop UI (appears intentional) | INFO | `src/xpst/knowledge/` | No |
| 6.1 | Scheduling | Fully functional end-to-end | INFO | `schedule_manager.py` | No |
| 6.2 | Scheduling | `launchctl unload` is deprecated on macOS 13+ | LOW | `cli.py:2673` | Yes — use `bootout` instead |
| 7.1 | Analytics | Fully functional; honest data; TikTok is best-effort via yt-dlp | INFO | `analytics.py` | No |
| 8.1 | OSS attribution | Comprehensive and correct; no gaps | INFO | `NOTICE.md`, `pyproject.toml` | No |

---

## Priority Fix Order

1. **HIGH — UI hover states** (§4.2): 6 pages need `containsMouse + ColorAnimation` applied to buttons, filter pills, dialog buttons, and form controls. Design pattern is already established in `Sidebar.qml` and `DashboardPage.qml`.

2. **MEDIUM — TikTok encoding** (§3.3): Add a TikTok video profile or minimum pre-flight validation (aspect ratio, duration) before upload so failures are caught locally.

3. **MEDIUM — Password prompts root cause** (§1.1): Investigate whether macOS Keychain access for platform credentials (keyring package) is the source of repeated prompts. If so, batch Keychain reads or document the "allow always" workaround.

4. **LOW — `launchctl unload` deprecation** (§6.2, §1.1): Replace with `launchctl bootout gui/{uid}/com.xpst.schedule` in uninstall path.

5. **LOW — `encode_for_platform()` footgun** (§2.2): Replace `ValueError` for TikTok/Threads/LinkedIn with passthrough to prevent future crash if called directly.
