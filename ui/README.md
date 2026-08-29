# xPST Web UI (Phase 1 foundation)

Svelte 5 + Vite 7 + Tailwind 4 SPA served by the FastAPI engine
(`xpst.dashboard.server`). Design tokens in `src/tokens.css` are ported
from the QML desktop theme (Apple-style light/dark ramp; dark mode via
`prefers-color-scheme`). Inter is self-hosted via `@fontsource-variable/inter`
(CSP-safe: no external font CDN).

## Build (required for the engine to serve the UI)

```bash
cd ui
npm install
npm run build      # → ui/dist
```

The FastAPI dashboard mounts `ui/dist` at `/` **when it exists**. Without a
build the engine falls back to its built-in string index page — the Tauri
shell still boots. `ui/dist` is gitignored and must NOT be committed;
packaging (Tauri sidecar) runs the build itself.

Set `XPST_UI_DIST=/path/to/dist` to serve a UI build from a non-standard
location (packaged installs).

## Develop (HMR against the local engine)

```bash
xpst ui &          # or: python -m xpst dashboard  (engine on 127.0.0.1:8080)
cd ui && npm run dev   # Vite dev server on 127.0.0.1:5173
```

The Vite dev server proxies `/api/*`, `/health`, `/state`, `/bio`,
`/metrics`, and `/oauth/callback` to `http://127.0.0.1:8080` (see
`vite.config.js`), so the app calls the same same-origin paths in dev and
production. The engine's Basic auth is handled by the browser natively.

## Layout

- `src/App.svelte` — shell: sidebar nav (Dashboard, Analytics, Videos,
  Accounts, Settings) + hash router
- `src/pages/*.svelte` — one page per nav item (Phase 1: Dashboard/Videos/
  Analytics/Accounts render live `/api` data; Settings renders the masked
  config view)
- `src/lib/api.js` — thin fetch client over the `/api` endpoints
- `src/tokens.css` — QML-port design tokens (no hardcoded hex in components)

## Backend contract

JSON endpoints (added in `src/xpst/dashboard/api.py`, Basic-auth protected,
never exempt): `GET /api/summary`, `GET /api/videos`,
`GET /api/videos/{video_id}`, `GET /api/health-status`, `GET /api/settings`.
Tests: `tests/test_web_ui_qa.py`.
