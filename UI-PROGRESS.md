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
- [ ] src/xpst/dashboard/api.py (APIRouter at /api: summary, videos, videos/{video_id}, health-status, settings)
- [ ] server.py: router included; ui/dist mounted at / when present, string-index fallback; CSP allows script-src 'self' only when UI mounted
- [ ] tests/test_web_ui_qa.py (401/200/shape per endpoint) + full suite + ruff check src tests
- [ ] ui/ scaffold (Vite+Svelte+Tailwind, sidebar shell, tokens.css from QML theme, Inter)
- [ ] npm run build green; ui/README.md build docs; ui/dist NOT committed
- [ ] PR + ci.yml run green
