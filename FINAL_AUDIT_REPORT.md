# xPST v1.0.0 — Final Security & Code-Quality Audit

**Date:** 2026-06-29
**Auditor:** Automated final audit (security + code quality)
**Scope:** Full source tree, docs, QML, git history, dependency & static analysis, credential hygiene, and resolution status of prior review findings.

---

## ⛔ OVERALL VERDICT: **BLOCKED**

The product **core** (CLI, desktop app, platform integrations, state/data integrity) is in strong shape: tests are green, the tree is PII-clean, there are no HIGH-severity static-analysis findings, and dependencies are clean. **The release is blocked by an unresolved security cluster in the MCP subsystem** that the project's own quality review already flagged as CRITICAL and that remains unfixed in shipped code:

- **M2 — Path traversal → credential disclosure** via MCP tool arguments (`workspace`, `video_id`). Concretely exploitable to read `~/.xpst/credentials/x_cookies.json`.
- **M1 — Fail-open MCP surface**: mutating tools (post/delete to *real* accounts) require **no auth and no confirmation by default**.

Two further CRITICAL-tier items remain open but are lower priority (P2 token-refresh never wired; R1 residual missing single-instance guard).

Checks 1–12 **PASS**. Check 13 **FAILS** (not all CRITICAL findings resolved). See the fix list at the end. With the MCP cluster fixed (or the MCP feature disabled-by-default and traversal closed), the remaining dimensions are release-ready.

---

## Check-by-check results

| # | Check | Result |
|---|-------|--------|
| 1 | No private/personal data in `src/xpst/**/*.py` | ✅ PASS |
| 2 | No private/personal data in docs & QML | ✅ PASS |
| 3 | No personal data in git history (diffs + messages) | ✅ PASS |
| 4 | All git authors = `xPST Contributors <xpst@opensource.local>` | ✅ PASS |
| 5 | Test suite passes | ✅ PASS (1427 passed, 3 skipped) |
| 6 | `ruff check src tests` | ✅ PASS |
| 7 | `mypy src/xpst` | ✅ PASS (107 files, no issues) |
| 8 | `bandit` HIGH-severity count | ✅ PASS (0 HIGH, 0 MEDIUM) |
| 9 | `pip-audit` | ✅ PASS (no known vulns) |
| 10 | Credential files encrypted or `0600` | ✅ PASS (all `0600`) |
| 11 | `.gitignore` excludes `graphify-out/`, `.venv/`, `build/`, `dist/` | ✅ PASS |
| 12 | No hardcoded API keys/secrets/tokens in source | ✅ PASS |
| 13 | All CRITICAL review findings resolved | ❌ **FAIL** (3 open + 1 residual) |

---

## Detailed findings

### 1–2. Personal data in source / docs / QML — ✅ PASS
- Targeted grep for `tylerjerman`, `jerman`, `invitenetworks`, `invite networks`, `mike@invite`, real home paths across `src/`, `docs/`, 15 `*.qml` files, `configs/`, `pyproject.toml`, build specs: **0 hits**.
- All emails in tracked files are placeholders: `release@example.com`, `t@e.com`, `your-email@gmail.com`. Project contact email `xpst@opensource.local` is intentional.
- All numeric IDs in docs/examples are obvious templates (e.g. `9000123456789012`, `7301234567890`, telegram `-1001234567890`).
- An independent deep semantic sweep (read-through of `__init__.py`, `AboutPage.qml`, `setup.py`, `config.py`, example data) found **no confirmed PII**.
- Positive control: the repo ships a deliberate guard test (`tests/test_repo_assets.py:96`) asserting the developer's real first name does **not** appear in `CONTRIBUTING.md`, and `src/xpst/diagnostics.py:62-63` redacts `/Users/<user>`, `/home/<user>`, `C:\Users\<user>` from diagnostic bundles.
- `src/xpst/__init__.py:35` → `__author__ = "xPST Contributors"`; `pyproject.toml` authors = `xPST Contributors`. No personal author/copyright metadata.

### 3. Git history — ✅ PASS
- 10 commits total. `git log --all -p` grep for personal markers: **0 matches** in all diffs and messages.
- Only generic test fixtures appear in history (`/Users/testuser`, `/Users/tester`); emails in diffs are decorators (`@click.option`, etc.), icon filenames (`icon_512x512@2x.png`), or placeholders.

### 4. Authors — ✅ PASS
- Every author **and** committer across all refs: `xPST Contributors <xpst@opensource.local>`. No other identity present.

### 5. Tests — ✅ PASS
- `pytest tests/ -q`: **1427 passed, 3 skipped** in ~104s, exit 0. (Skips are optional embeddings/smoke tests.)

### 6. Ruff — ✅ PASS
- `ruff check src tests` → "All checks passed!"

### 7. Mypy — ✅ PASS
- `mypy src/xpst` → "Success: no issues found in 107 source files." (Only 3 informational `annotation-unchecked` notes — not errors.)

### 8. Bandit — ✅ PASS (HIGH = 0)
- 125 total issues, **all LOW severity**; **0 HIGH, 0 MEDIUM**.
- Breakdown: B603 subprocess (48, expected — no `shell=True`), B110 try/except/pass (26), B404 import subprocess (17), B607 partial path (16), B311 non-crypto random (9, jitter), **B105 "hardcoded_password_string" (9)**.
- All 9 B105 hits are **empty-string config schema defaults** (`"client_secret": ""`, `"bot_token": ""`, `"dashboard_password_hash": ""`, …) in `config.py`/`config_migration.py` — confirmed false positives, not secrets.

### 9. pip-audit — ✅ PASS
- "No known vulnerabilities found." Only `xpst` (1.0.0) itself skipped — not on PyPI, expected for a local package.

### 10. Credential file permissions — ✅ PASS
- All 10 files in `~/.xpst/credentials/` are `-rw-------` (`0600`): `.fallback_salt`, `.fallback_secret`, `_keyring_index.json`, `instagram_session.enc`, `instagram_session.json`, `instagram_session_full.json`, `tiktok_cookies.enc`, `x_cookies.enc`, `x_cookies.json`. Meets the "encrypted **or** 0600" criterion.
- **Observation (not a repo leak; outside the repo):** plaintext `*.json` session/cookie files (`x_cookies.json`, `instagram_session*.json`) exist alongside `.enc` versions and rely on filesystem perms alone. This is the exact data exposed by finding **M2** below — see fix list.

### 11. .gitignore — ✅ PASS
- Excludes `graphify-out/`, `.venv/`, `build/`, `dist/` (also `release/`, `sbom.json`, `.xpst/`, `.crosspstr/`, `*.cookies`, `*session*.json`, state files, `docs/internal/`, `docs/research/`).
- `git ls-files` shows **no** tracked build artifacts (`dist/`, `build/`, `release/`, `graphify-out/`, `sbom.json`, `.coverage` all untracked).

### 12. Hardcoded secrets in source — ✅ PASS
- No matches for private keys, AWS/GitHub/OpenAI/Google/Slack token patterns, or non-empty `secret/token/password=...` assignments in `src/`.
- The repo's own scanner `scripts/scan_public_safety.py` (used by the test suite) reports **OK: True, 298 files scanned, 0 findings** against git-publishable files.

### 13. CRITICAL review findings — ❌ FAIL (not all resolved)

The three review docs are point-in-time **read-only** audits ("No code was modified"). Two later commits actually applied fixes — `04ed7c7 fix: critical bugs from quality review` and `68dca5 fix: gap analysis` — so each finding was re-verified **against current code**, not the docs' stale status.

**QUALITY_REVIEW.md — 8 CRITICAL findings: 4 resolved, 1 mitigated (residual), 3 OPEN**

| ID | Title | file:line | Status (verified in code) |
|----|-------|-----------|---------------------------|
| Q1 | `ContentPage.qml` unclosed `captionForPost` fn → page fails to load | `ContentPage.qml:182` | ✅ **RESOLVED** — stub removed in 04ed7c7 |
| Q2 | `DetailPanel.qml` objects inside bare `if {}` (invalid QML) | `DetailPanel.qml:280,…` | ✅ **RESOLVED** — replaced with `visible:` (3 sites) |
| Q3 | `main.qml:592` calls non-existent `controller.getFileInfo` | `backend.py:1605` | ✅ **RESOLVED** — `getFileInfo` slot added |
| P1 | TikTok/LinkedIn `f.read()` whole video into RAM → OOM | `tiktok.py:222`, `linkedin.py:206` | ✅ **RESOLVED** — now stream `content=open(path,"rb")` |
| R1 | Cross-process lost update on `state.json`; pidfile unused | `state_store.py:369-394`, `engine.py:218` | ⚠️ **MITIGATED + residual** (see below) |
| **M1** | **MCP fail-open**: mutating tools need no auth/confirm by default | `mcp/server.py:608-641` | ❌ **OPEN** |
| **M2** | **Path traversal** via MCP `workspace` / `video_id` args | `knowledge/workspace.py:24`, `mcp/tools.py:41,100,121`, `mcp/server.py:1135-1141` | ❌ **OPEN** |
| **P2** | TikTok/Threads `_refresh_access_token` exists but never called | `tiktok.py:101`, `threads.py:106` | ❌ **OPEN** |

> Also fixed in 04ed7c7 (verified): **Q4** `AnalyticsPage.qml` missing brace, **C1** crash-recovery 6-platform coverage, **S1** `schedule.json` atomic write.

**R1 — mitigated, with a residual:** `state_store.py` performs read-modify-write under a thread lock **and** a cross-process `fcntl.flock(LOCK_EX)`, then writes atomically (temp + `os.replace`). This prevents state corruption / lost updates across processes — the core R1 data-loss risk is closed. **Residual:** `CrossPostEngine.acquire_pidfile()` (`engine.py:218`) still has **zero callers**, so there is no single-instance guard; two concurrently-running engines could double-post (state stays consistent, but the same video could be posted twice).

**GAP_ANALYSIS.md — 0 CRITICAL** (verdict "No CRITICAL data-loss or crash bugs"). Its 7 HIGH items were largely addressed by `68dca5` (verified): CLI `connect`/`auth` now accepts all 6 platforms (`cli.py`), SchedulePage Threads/LinkedIn scheduling, `state_manager` 6-platform stats, full `docs/setup-linkedin.md`, README env-var scheme corrected, Docker documented as shipped, dead `utils/retry_policy.py` removed.

**GRAPHIFY_REVIEW.md — 0 product-code CRITICAL.** One 🔴 is a *tooling* defect: `src/xpst/utils/credentials.py` produced zero nodes in the dependency graph, so a graph-driven security review would silently skip the credential module — relevant because credential code is exactly where M1/M2 impact lands. Non-blocking for the product, but worth noting given this is a security audit.

---

## 🔧 Required fixes before release (prioritized)

### P0 — Security blockers (MCP subsystem)

1. **M2 — Close path traversal in workspace/transcript resolution.**
   - `src/xpst/knowledge/workspace.py:20-27` — validate `name` (reject `/`, `\`, `..`, leading `~`; allow only a safe charset) and assert the resolved path is contained: `root.resolve().is_relative_to(_xpst_home()/"knowledge")`.
   - `src/xpst/knowledge/workspace.py:54-81` (`save_transcript`/`get_transcript`) — sanitize `content_hash` the same way before building `transcripts_dir / f"{hash}.json"`.
   - `src/xpst/mcp/server.py:1135-1141` — sanitize `video_id` before passing to `get_transcript` (current code lets `video_id="../../../credentials/x_cookies"` read `~/.xpst/credentials/x_cookies.json` and return its contents).
   - Add regression tests for `..`, absolute paths, and symlink escapes on both the `workspace` and `video_id` arguments.

2. **M1 — Make the MCP mutating surface secure-by-default.**
   - `src/xpst/mcp/server.py:608-641` — flip the default so `xpst_post`/`xpst_delete`/`xpst_backfill`/`kb_add`/… require confirmation (or an explicit opt-in token) **unless** an env var disables it, rather than the current opt-in `XPST_MCP_REQUIRE_CONFIRM`. At minimum, document the fail-open posture prominently in `SECURITY.md`/README and have the server log a warning at startup when running unguarded.

### P1 — Resilience / robustness

3. **P2 — Wire up token refresh.** Call `_refresh_access_token()` on `401`/`*_AUTH_EXPIRED` during upload in `src/xpst/platforms/tiktok.py` and `src/xpst/platforms/threads.py` (and verify LinkedIn), then retry once. Add a test simulating a mid-upload 401 with a valid refresh token.

4. **R1 residual — Enforce single instance.** Call `engine.acquire_pidfile()` on startup of long-running entry points (CLI `run`, scheduler, desktop backend) and `release_pidfile()` on shutdown, surfacing `PidfileLockError` as a clean "already running" message.

### P2 — Cleanup (non-blocking)

5. Remove the stray literal **`~/.xpst/backups/`** directory committed at the repo root (`/Users/tylerjerman/XPST/~/`) — created by a tilde-expansion bug; untracked and empty, but should not exist. (`rm -rf '/Users/tylerjerman/XPST/~'`)
6. Encrypt the plaintext credential JSONs (`x_cookies.json`, `instagram_session*.json`) at rest instead of relying on `0600` alone (defense-in-depth; also blunts M2's read impact).
7. Root-cause the graphify extractor miss on `utils/credentials.py` (GRAPHIFY 🔴) before relying on the dependency graph for future security review.

---

## Evidence appendix (commands run)

- `git log --all --format="%an <%ae>"` / `%cn <%ce>` → single identity (check 4)
- `git log --all -p | grep -niE '<personal markers>'` → 0 (check 3)
- `pytest tests/ -q --tb=short` → 1427 passed, 3 skipped (check 5)
- `ruff check src tests` → pass (check 6)
- `mypy src/xpst` → no issues, 107 files (check 7)
- `bandit -r src/xpst/ -f json` → 0 HIGH / 0 MEDIUM / 125 LOW (check 8)
- `pip-audit` → no known vulns (check 9)
- `ls -l ~/.xpst/credentials/` → all `0600` (check 10)
- `cat .gitignore` + `git ls-files | grep -E '(dist|build|release|graphify-out)/'` → excluded, none tracked (check 11)
- `python scripts/scan_public_safety.py --json` → `{ok: true, scanned_files: 298, findings: 0}` (checks 1/2/12)
- Manual code reads: `state_store.py`, `mcp/server.py`, `knowledge/workspace.py`, `knowledge/mcp/tools.py`, `platforms/tiktok.py`, `platforms/linkedin.py`, QML diffs of `04ed7c7` (check 13)

---

*Bottom line: ship-quality everywhere except the MCP subsystem. Fix M2 and M1 (P0), then re-audit the MCP surface; P2/R1 should follow but are not, on their own, release-blocking.*
