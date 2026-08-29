# UI-PROGRESS.md — Web UI Phase 1 foundation (branch feat/web-ui-foundation)

Task: scaffold ui/ (Vite+Svelte+Tailwind), thin FastAPI /api routers, mount built UI,
tests, PR + green CI. Basis: ~/xPST-work/ui-assess/ASSESSMENT.md (decision (b)).

## Status log

- [x] Repo verified: /Users/itxji/xPST-work/pr92/repo, branch feat/web-ui-foundation cut at
      github/main = 98abba9 (post-merge). NOTE: remote is named `github` (not `origin`) in
      this clone; fetch refspec limited — fetched main via `git fetch github main`.
- [x] API surface discovery:
      - get_summary_stats            → src/xpst/dashboard/analytics.py:925 (memoized by cached_summary_stats:119)
      - get_video_lineup             → src/xpst/dashboard/analytics.py:602
      - AnalyticsStore               → src/xpst/analytics_store.py:66 (platform_totals:137, latest_for_post:363, history:169)
      - collect_live_auth_status     → src/xpst/auth_status.py:290 (async core :237)
      - config masking               → xpst.cli._mask_sensitive_values (cli.py:2295), reused by MCP config_show (mcp/server.py:1354)
      - **AnalyticsStore.get_video_metrics_map / get_video_metrics DO NOT exist on this branch** (grep-verified)
        → /api/videos uses lineup + platform_totals rollup; /api/videos/{id} uses lineup filter +
          AnalyticsStore.latest_for_post + history.
- [x] src/xpst/dashboard/api.py (APIRouter at /api: summary, videos, videos/{video_id}, health-status, settings)
- [x] server.py: router included; ui/dist mounted at / when present (XPST_UI_DIST override), string-index fallback; CSP script-src 'self' only when UI mounted
- [x] tests/test_web_ui_qa.py — 26 tests, all passing (401 anonymous, 401 bad creds, 200+shape, masking, mount/fallback)
- [x] Existing dashboard tests pinned to no-UI fallback mode (XPST_UI_DIST) so local ui/dist doesn't flip them
- [x] ui/ scaffold (Vite 7 + Svelte 5 + Tailwind 4, sidebar shell, tokens.css from QML theme, Inter self-hosted)
- [x] npm run build green (270ms, 47.5KB JS gzip 17.2KB); ui/README.md build docs; ui/dist gitignored + NOT committed
- [x] Isolated full-suite + ruff verification (worktree — main clone has concurrent sibling edits):
      2089 passed, 2 skipped (3m50s); `ruff check src tests` → "All checks passed!"
- [x] Committed 3ba1565 (only my 21 files; concurrent agents' engine/mcp/state_store edits left uncommitted & untouched)
- [x] PR #96 opened: https://github.com/TysAIs/xPST/pull/96 (NOT merged)
- [x] CI green on branch tip:
      - dispatch run 33236943664 → completed/success
      - PR run 33236968958 → completed/success
      (at commit 1bb0e80; final evidence commit re-dispatched)

## Concurrency note
While working, a sibling agent modified engine.py / mcp/server.py / state_store.py /
test_adversarial_data_loss.py in the SAME clone (mid-write state_store broke one ruff
scan; compiles fine now). Mitigation: committed only my files, full-suite verified in an
isolated git worktree at HEAD 3ba1565.
