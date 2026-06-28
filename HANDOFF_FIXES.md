# xPST Fix Handoff — Blocker & Non-Blocker Solutions

> **Context**: This document captures every issue from the 2026-06-12 Ship Readiness Audit with concrete, implementable fixes. No personal info — just repo-relative paths, commands, and patterns. Any agent (Fable 5, Claude, Codex, etc.) can pick this up and execute.

---

## 📋 Quick Reference: Issue Index

| # | Category | Severity | Status | Fix Size |
|---|----------|----------|--------|----------|
| 1 | Release artifact collision | Blocker | ⏳ Open | Medium (workflow + script) |
| 2 | Unsigned desktop artifacts | Blocker | ⏳ Open | Policy + infra |
| 3 | `health`/`dry-run` crash on missing FFmpeg | Blocker | ✅ **Done** | Small (CLI guards) |
| 4 | Desktop "Schedule New" toast only | Blocker | ⏳ Open | Medium (QML + backend) |
| 5 | No live-platform smoke evidence | Blocker | ⏳ Open | Creds-dependent |
| 6 | README assumes `uv` | Non-blocker | ⏳ Open | Tiny (docs) |
| 7 | KB commands missing `--json` | Non-blocker | ✅ **Done** (doctor) | Tiny (CLI flags) |
| 8 | `XPST_CONFIG_DIR` override unclear | Non-blocker | ⏳ Open | Small (config + docs) |

---

## 🔴 BLOCKER 1 — Release Artifact Collision (Invalid RELEASE_EVIDENCE.json, Incomplete Checksums)

**Root Cause**: `.github/workflows/release.yml` runs `scripts/release_artifacts.py` in three parallel lanes (Python, Windows, Linux). Each lane writes **same-named files**: `RELEASE_EVIDENCE.json`, `SHA256SUMS`, `SHA512SUMS`, `pypi.json`. The release job downloads artifacts with `merge-multiple: true` → last writer wins, corrupting JSON and losing assets.

**Files to Change**:
- `.github/workflows/release.yml` (lines 44, 116, 158, 214, 217, 222)
- `scripts/release_artifacts.py` (lines 229, 249, 251, 264, 284, 457)

**Fix Strategy**: Namespace lane outputs → aggregate after merge.

### Step 1 — Lane-level namespacing (release_artifacts.py)
```python
# In each lane's emit step, prefix artifact names:
# Python lane  →  RELEASE_EVIDENCE.python.json, SHA256SUMS.python, etc.
# Windows lane →  RELEASE_EVIDENCE.windows.json, SHA256SUMS.windows, etc.
# Linux lane   →  RELEASE_EVIDENCE.linux.json, SHA256SUMS.linux, etc.
```
Change `scripts/release_artifacts.py:write_artifact()` to accept a `lane_suffix` and write `f"{basename}.{lane_suffix}"`.

### Step 2 — Aggregate step (new job in release.yml)
Add a final job `aggregate-release-manifest` that:
1. Downloads all three lane artifact sets
2. Reads all `RELEASE_EVIDENCE.*.json` → merges into single `RELEASE_EVIDENCE.json`
3. Concatenates all `SHA256SUMS.*` → single `SHA256SUMS` (one line per asset)
4. Same for `SHA512SUMS.*` and `pypi.json`
5. Uploads aggregated files as **final release artifacts**

### Step 3 — Release job depends on aggregate
```yaml
# release.yml
jobs:
  release:
    needs: [python-build, windows-build, linux-build, aggregate-release-manifest]
    # download aggregated artifacts only
```

**Verification**:
```bash
gh release download v0.1.0-rc --pattern RELEASE_EVIDENCE.json --pattern SHA256SUMS
cat RELEASE_EVIDENCE.json | jq .  # valid JSON
wc -l SHA256SUMS  # should equal total published assets (wheel, sdist, exe, dmg, sbom, docs)
```

---

## 🔴 BLOCKER 2 — Unsigned Desktop Artifacts

**Current State**: Windows exe downloads and runs, but `verify_windows_exe.py --require-signed` fails (no Authenticode). macOS DMG similarly unsigned/notarized. Release is marked as normal (not draft/prerelease).

**Decision Required** (pick one):
| Option | Effort | When to Ship |
|--------|--------|--------------|
| **A. Mark RCs as prerelease/unsigned** | 1 line in release.yml | Immediately |
| **B. Full signing + notarization** | Days-weeks (certs, CI secrets, Apple Dev account) | Before public GA |

### Option A — Prerelease Flag (fastest)
```yaml
# .github/workflows/release.yml
# In the release creation step:
draft: false
prerelease: true  # <-- add this
generate_release_notes: true
```
Update `scripts/release_preflight.py` to warn if `prerelease: false` but artifacts unsigned.

### Option B — Signing Infrastructure (outline)
**Windows (Authenticode)**:
- Get EV code-signing cert (DigiCert, Sectigo, etc.) → store in GitHub Secrets (`WINDOWS_CERT_P12`, `WINDOWS_CERT_PASSWORD`)
- Use `signtool` in Windows build lane: `signtool sign /f cert.p12 /p $PWD /tr http://timestamp.digicert.com /td sha256 /fd sha256 xPST.exe`
- Verify: `signtool verify /pa /v xPST.exe`

**macOS (Developer ID + Notarization)**:
- Apple Developer Program ($99/yr) → Developer ID Application cert
- Store base64 cert + keychain password in secrets
- Sign app bundle: `codesign --deep --force --verify --verbose --options runtime --sign "Developer ID Application: ..." xPST.app`
- Notarize: `xcrun notarytool submit xPST.dmg --apple-id $APPLE_ID --team-id $TEAM_ID --password $NOTARY_PASSWORD --wait`
- Staple: `xcrun stapler staple xPST.dmg`

**CI Integration**: Add signing steps in respective build lanes *before* artifact upload. Update `verify_windows_exe.py` / `verify_macos_artifact.py` to check signatures.

**Verification**:
```bash
# Windows
python scripts/verify_windows_exe.py --path xPST.exe --require-signed --json

# macOS
python scripts/verify_macos_artifact.py --path xPST.dmg --require-notarized --json
```

---

## 🟢 BLOCKER 3 — `health`/`dry-run` Crash on Missing FFmpeg — **DONE**

**Fix Applied** (for reference — already in `main`):
```python
# src/xpst/cli.py — added import
from xpst.utils.video import FFmpegNotFoundError

# In health() and run() — wrap engine creation:
try:
    engine = CrossPostEngine(config)
except FFmpegNotFoundError as e:
    if as_json:
        json_output({
            "error": "helper_tool_missing",
            "tool": "ffmpeg",
            "message": str(e),
            "hint": e.hint,
        }, True)
        return
    raise
```

**Test**:
```bash
# On machine without FFmpeg
xpst health --json      # → {"error": "helper_tool_missing", "tool": "ffmpeg", ...}
xpst run --dry-run --json  # → {"error": "helper_tool_missing", "tool": "ffmpeg", ...}
```

---

## 🔴 BLOCKER 4 — Desktop "Schedule New" Toast Only

**Current State**: `SchedulePage.qml:173` shows `showToast("Schedule New - coming soon", false)`. Backend `ScheduleManager.add()` exists and works.

**Files to Wire**:
- `src/xpst/desktop_app/qml/pages/SchedulePage.qml` (UI)
- `src/xpst/desktop_app/backend.py` (DesktopBackend — add slot)
- `src/xpst/schedule_manager.py` (already has `add()`)

### Step 1 — Add DesktopBackend Slot
```python
# src/xpst/desktop_app/backend.py
@Slot(str, str, str, str, result=str)  # video_path, caption, scheduled_time_iso, platforms_json
def scheduleNew(self, video_path: str, caption: str, scheduled_time: str, platforms_json: str) -> str:
    """Create a new scheduled post from the desktop UI."""
    try:
        import json
        from datetime import datetime
        from xpst.schedule_manager import ScheduleManager
        from xpst.utils.platform import get_config_dir

        config_dir = get_config_dir()
        sched = ScheduleManager(config_dir)

        platforms = json.loads(platforms_json) if platforms_json else ["youtube", "instagram", "x"]
        dt = datetime.fromisoformat(scheduled_time)

        entry = sched.add(
            video_path=video_path,
            caption=caption,
            scheduled_time=dt,
            platforms=platforms,
        )
        return json.dumps({"ok": True, "entry": entry, "id": entry["id"]})
    except Exception as e:
        logger.error("scheduleNew failed: %s", e)
        return json.dumps({"ok": False, "error": str(e)})
```

### Step 2 — Wire QML Action
```qml
// src/xpst/desktop_app/qml/pages/SchedulePage.qml — replace toast line (173)
onClicked: {
    dayPostsPopup.close()
    // Open a dialog or inline form to collect: video_path, caption, datetime, platforms
    // Then call controller.scheduleNew(...)
    // For MVP: simple prompt sequence or delegate to Content page
    showToast("Opening schedule creator...", false)
    // controller.scheduleNew(videoPath, caption, isoTime, platformsJson)
}
```
**MVP Approach**: Add a "Schedule New" button that navigates to Content page with a `mode="schedule"` flag, or show a minimal inline dialog (QML `Dialog` + `TextField` + `DateTimePicker`).

### Step 3 — Expose to QML
In `main.qml` controller connections, ensure `scheduleNew` is callable (like `runPost`, `getHealth`).

**Verification**:
```bash
# Run desktop app, go to Schedule page, click "Schedule New"
# Fill form → entry appears in calendar + ~/.xpst/schedule.json
python -c "from xpst.schedule_manager import ScheduleManager; import json; sm=ScheduleManager(); print(json.dumps(sm.list(), indent=2))"
```

---

## 🔴 BLOCKER 5 — No Live-Platform Smoke Evidence

**Requirement**: Owner-approved test uploads to YouTube, Instagram, X with: upload → analytics/health → retry/backfill → delete/cleanup.

**Files to Extend**:
- `scripts/verify_live_platforms.py` (already exists, needs creds)
- `tests/test_live_platform_smoke.py` (exists, skips without creds)

### Credential Setup (one-time, owner-only)
```bash
# YouTube: OAuth client_secrets.json → ~/.xpst/credentials/youtube_client_secrets.json
# X: cookies export → ~/.xpst/credentials/x_cookies.json
# Instagram: session file → ~/.xpst/credentials/instagram_session.json
```

### Smoke Script Pattern (extend verify_live_platforms.py)
```python
# For each platform:
# 1. Upload a small test video (use a local fixture)
# 2. Verify upload succeeds + get media ID
# 3. Call analytics/health for that media
# 4. Wait/retry if needed (circuit breaker test)
# 5. Delete/cleanup the test media
# 6. Report JSON summary
```

**Run**:
```bash
python scripts/verify_live_platforms.py --platforms youtube,instagram,x --json
```

**CI Gate**: Add as optional job in `.github/workflows/ci.yml` triggered manually (`workflow_dispatch`) with secrets.

---

## 🟡 NON-BLOCKER 6 — README Assumes `uv`

**Fix**: Make plain `pip` the primary path until signed binary installers exist.

**File**: `README.md` (lines 132, 137, 364)

**Change**:
```markdown
## Install

### Option 1: pip (works everywhere)
```bash
pip install -e ".[full,dev]"
```

### Option 2: uv (faster, if installed)
```bash
uv pip install -e ".[full,dev]"
```
```

---

## 🟢 NON-BLOCKER 7 — KB Commands Missing `--json` — **PARTIAL (doctor done)**

**Remaining**: `kb areas`, `kb course` need `--json` flag.

**Pattern** (copy from `kb_doctor`):
```python
@kb.command("areas")
@click.option("--workspace", "-w", default="default")
@click.option("--json", "as_json", is_flag=True)
def kb_areas(workspace: str, as_json: bool):
    # ... existing logic ...
    if as_json:
        console.print_json(json.dumps({"areas": areas_list}))
        return
    # ... existing rich output ...
```

**Files**: `src/xpst/knowledge/cli_kb.py` (search for `kb_areas`, `kb_course`)

---

## 🟡 NON-BLOCKER 8 — `XPST_CONFIG_DIR` Override Unclear

**Issue**: Setting `XPST_CONFIG_DIR` doesn't move config dir; commands still use `~/.xpst`.

**Fix**: Centralize config dir resolution in one place, respect env var everywhere.

### Step 1 — Central Resolver
```python
# src/xpst/utils/platform.py (or new config_dir.py)
import os
from pathlib import Path

def get_config_dir() -> Path:
    """Resolve xPST config directory.
    Priority: XPST_CONFIG_DIR > XPST_HOME > ~/.xpst
    """
    if env := os.environ.get("XPST_CONFIG_DIR"):
        return Path(env).expanduser()
    if env := os.environ.get("XPST_HOME"):
        return Path(env).expanduser() / ".xpst"
    return Path.home() / ".xpst"
```

### Step 2 — Replace All Direct `~/.xpst` References
Search/replace across codebase:
- `Path("~/.xpst").expanduser()` → `get_config_dir()`
- `Path.home() / ".xpst"` → `get_config_dir()`
- `os.path.expanduser("~/.xpst")` → `str(get_config_dir())`

**Key Files** (grep `\\.xpst`):
- `src/xpst/cli.py` (`load_config`, `_session_health`)
- `src/xpst/engine.py` (config_dir passed to components)
- `src/xpst/state.py`, `state_manager.py`, `state_store.py`
- `src/xpst/schedule_manager.py`
- `src/xpst/desktop_app/backend.py`
- `src/xpst/knowledge/workspace.py`
- `tests/` (use `tmp_path` fixture instead)

### Step 3 — Document
Add to `README.md` and `docs/CONFIGURATION.md`:
```markdown
## Config Directory Override
Set `XPST_CONFIG_DIR=/custom/path` to relocate all xPST data (config, credentials, schedule, state, logs).
```

**Test**:
```bash
XPST_CONFIG_DIR=/tmp/xpst-test xpst setup --non-interactive
ls /tmp/xpst-test/  # should have credentials/, downloads/, logs/, backups/, config.yaml, state.json, schedule.json
```

---

## 🧪 Verification Checklist (Run After Each Fix)

```bash
# 1. Core package
cd /path/to/xPST
python -m pytest tests/ -q --tb=short  # 1239 passed, 4 skipped (usecases failure ok)

# 2. Lint/type
ruff check src tests
mypy src/xpst
lint-imports

# 3. CLI JSON contracts
xpst version --json | jq .
xpst providers --json | jq .
xpst readiness --json | jq .
xpst health --json | jq .       # no traceback even without FFmpeg
xpst run --dry-run --json | jq . # no traceback even without FFmpeg
xpst status --json | jq .
xpst analytics --json | jq .
xpst diagnostics --json | jq .
xpst update --components --json | jq .
xpst kb doctor --json | jq .     # NEW: works

# 4. Desktop smoke
QT_QPA_PLATFORM=offscreen python scripts/verify_qml_pages.py

# 5. Package build
python scripts/build_package.py
python scripts/clean_install_smoke.py --dist dist --artifact both

# 6. Release preflight
python scripts/release_preflight.py --json

# 7. Graphify refresh (after code changes)
graphify update . --no-viz
```

---

## 📁 File Index for Quick Navigation

| Area | Files |
|------|-------|
| Release workflow | `.github/workflows/release.yml`, `scripts/release_artifacts.py` |
| Signing scripts | `scripts/sign_windows.ps1`, `scripts/sign_macos.sh`, `scripts/verify_windows_exe.py`, `scripts/verify_macos_artifact.py` |
| CLI entry points | `src/xpst/cli.py` |
| Desktop backend | `src/xpst/desktop_app/backend.py` |
| Schedule UI | `src/xpst/desktop_app/qml/pages/SchedulePage.qml` |
| Schedule logic | `src/xpst/schedule_manager.py` |
| KB CLI | `src/xpst/knowledge/cli_kb.py` |
| Config dir resolution | `src/xpst/utils/platform.py` (add `get_config_dir`) |
| Live platform verify | `scripts/verify_live_platforms.py` |
| Graphify output | `graphify-out/` (commit this) |

---

## 🎯 Suggested Execution Order

1. **Release artifact aggregation** (Blocker 1) — unblocks valid releases
2. **Decide signing policy** (Blocker 2) — prerelease flag is 1 line; do it now
3. **Wire Schedule New** (Blocker 4) — backend exists, QML is the gap
4. **Config dir override** (Non-blocker 8) — touches many files, do early
5. **KB `--json` for areas/course** (Non-blocker 7) — trivial
6. **README pip-first** (Non-blocker 6) — trivial
7. **Live platform smoke** (Blocker 5) — requires owner creds, do when available

---

## 🤖 Agent Handoff Notes

- **Graphify map** is in `graphify-out/` at commit `ba96505`. Refresh with `graphify update . --no-viz` after any code change.
- **Test suite** passes (1 failure = pre-existing dead-code check for `src/xpst/usecases/` — ignore or delete that dir).
- **No secrets in repo** — all creds via env/secrets.
- **Windows paths** in audit (`C:\\Users\\user\\xPST-latest-audit`) are from auditor's machine; use `~/xPST` or `/tmp/xPST` on Unix.
- **Python toolchain**: 3.11+, `uv` optional, `pip install -e ".[full,dev]"` works.

---

*Generated 2026-06-12 from Ship Readiness Audit. Update this file as fixes land.*